from functools import partial
from tenacity import AsyncRetrying, stop_after_attempt, wait_random
from typing import List
from web3 import Web3
from web3.datastructures import AttributeDict
from eth_utils import keccak
from web3._utils.events import get_event_data
from eth_abi.codec import ABICodec
import asyncio
import aiohttp
from redis_conn import provide_async_redis_conn_insta
from dynaconf import settings
import logging.config
import os
import math
import json
from bounded_pool_executor import BoundedThreadPoolExecutor
import threading
from concurrent.futures import as_completed
import aioredis
from async_limits.strategies import AsyncFixedWindowRateLimiter
from async_limits.storage import AsyncRedisStorage
from async_limits import parse_many as limit_parse_many
import time
from datetime import datetime, timedelta
from redis_keys import (
    uniswap_pair_contract_tokens_addresses, uniswap_pair_contract_tokens_data, uniswap_pair_cached_token_price,
    uniswap_pair_contract_V2_pair_data, uniswap_pair_cached_block_height_token_price
)
from helper_functions import (
    acquire_threading_semaphore
)

w3 = Web3(Web3.HTTPProvider(settings.RPC.MATIC[0]))
# TODO: Use async http provider once it is considered stable by the web3.py project maintainers
# web3_async = Web3(Web3.AsyncHTTPProvider(settings.RPC.MATIC[0]))

logger = logging.getLogger('PowerLoom|UniswapHelpers')
logger.setLevel(logging.DEBUG)
logger.handlers = [logging.handlers.SocketHandler(host='localhost', port=logging.handlers.DEFAULT_TCP_LOGGING_PORT)]

# Initialize rate limits when program starts
GLOBAL_RPC_RATE_LIMIT_STR = settings.RPC.rate_limit
PARSED_LIMITS = limit_parse_many(GLOBAL_RPC_RATE_LIMIT_STR)

# # # RATE LIMITER LUA SCRIPTS
SCRIPT_CLEAR_KEYS = """
        local keys = redis.call('keys', KEYS[1])
        local res = 0
        for i=1,#keys,5000 do
            res = res + redis.call(
                'del', unpack(keys, i, math.min(i+4999, #keys))
            )
        end
        return res
        """

SCRIPT_INCR_EXPIRE = """
        local current
        current = redis.call("incrby",KEYS[1],ARGV[2])
        if tonumber(current) == tonumber(ARGV[2]) then
            redis.call("expire",KEYS[1],ARGV[1])
        end
        return current
    """

# args = [value, expiry]
SCRIPT_SET_EXPIRE = """
    local keyttl = redis.call('TTL', KEYS[1])
    local current
    current = redis.call('SET', KEYS[1], ARGV[1])
    if keyttl == -2 then
        redis.call('EXPIRE', KEYS[1], ARGV[2])
    elseif keyttl ~= -1 then
        redis.call('EXPIRE', KEYS[1], keyttl)
    end
    return current
"""


# # # END RATE LIMITER LUA SCRIPTS


# KEEP INTERFACE ABIs CACHED IN MEMORY
def read_json_file(file_path: str):
    """Read given json file and return its content as a dictionary."""
    try:
        f_ = open(file_path, 'r')
    except Exception as e:
        logger.warning(f"Unable to open the {file_path} file")
        logger.error(e, exc_info=True)
        raise e
    else:
        json_data = json.loads(f_.read())
    return json_data


pair_contract_abi = read_json_file(f"abis/UniswapV2Pair.json")
erc20_abi = read_json_file('abis/IERC20.json')
router_contract_abi = read_json_file(f"abis/UniswapV2Router.json")
uniswap_trade_events_abi = read_json_file('abis/UniswapTradeEvents.json')

router_addr = settings.CONTRACT_ADDRESSES.IUNISWAP_V2_ROUTER
dai = settings.CONTRACT_ADDRESSES.DAI
usdt = settings.CONTRACT_ADDRESSES.USDT
weth = settings.CONTRACT_ADDRESSES.WETH

codec: ABICodec = w3.codec

UNISWAP_TRADE_EVENT_SIGS = {
    'Swap': "Swap(address,uint256,uint256,uint256,uint256,address)",
    'Mint': "Mint(address,uint256,uint256)",
    'Burn': "Burn(address,uint256,uint256,address)"
}


class RPCException(Exception):
    def __init__(self, request, response, underlying_exception, extra_info):
        self.request = request
        self.response = response
        self.underlying_exception: Exception = underlying_exception
        self.extra_info = extra_info

    def __str__(self):
        ret = {
            'request': self.request,
            'response': self.response,
            'extra_info': self.extra_info,
            'exception': None
        }
        if isinstance(self.underlying_exception, Exception):
            ret.update({'exception': self.underlying_exception.__str__()})
        return json.dumps(ret)

    def __repr__(self):
        return self.__str__()


# needs to be run only once
async def load_rate_limiter_scripts(redis_conn: aioredis.Redis):
    script_clear_keys_sha = await redis_conn.script_load(SCRIPT_CLEAR_KEYS)
    script_incr_expire = await redis_conn.script_load(SCRIPT_INCR_EXPIRE)
    LUA_SCRIPT_SHAS = {
        "script_incr_expire": script_incr_expire,
        "script_clear_keys": script_clear_keys_sha
    }
    return LUA_SCRIPT_SHAS


# initiate all contracts
try:
    # instantiate UniswapV2Factory contract (using quick swap v2 factory address)
    quick_swap_uniswap_v2_factory_contract = w3.eth.contract(
        address=Web3.toChecksumAddress(settings.CONTRACT_ADDRESSES.IUNISWAP_V2_FACTORY),
        abi=read_json_file('./abis/IUniswapV2Factory.json')
    )

except Exception as e:
    quick_swap_uniswap_v2_factory_contract = None
    logger.error(e, exc_info=True)


def get_event_sig_and_abi(event_name):
    event_sig = '0x' + keccak(text=UNISWAP_TRADE_EVENT_SIGS.get(event_name, 'incorrect event name')).hex()
    abi = uniswap_trade_events_abi.get(event_name, 'incorrect event name')
    return event_sig, abi


def get_events_logs(contract_address, toBlock, fromBlock, topics, event_abi):
    event_log = w3.eth.get_logs({
        'address': Web3.toChecksumAddress(contract_address),
        'toBlock': toBlock,
        'fromBlock': fromBlock,
        'topics': topics
    })

    all_events = []
    for log in event_log:
        evt = get_event_data(codec, event_abi, log)
        all_events.append(evt)

    return all_events

async def get_block_details(ev_loop, block_number):
    try:
        block_details = dict()
        block_det_func = partial(w3.eth.get_block, int(block_number))
        block_details = await ev_loop.run_in_executor(func=block_det_func, executor=None)
        block_details = dict() if not block_details else block_details
    except Exception as e:
        logger.error('Error attempting to get block details of recent transaction timestamp %s: %s', block_number, e, exc_info=True)
        block_details = dict()
    finally:
        return block_details

async def store_price_at_block_range(begin_block, end_block, token0, token1, price, redis_conn: aioredis.Redis):
    """Store price at block range in redis."""

    block_prices = {}
    for i in range(begin_block, end_block + 1):
        block_prices[json.dumps({
            "price": price,
            "block_number": i,
            "timestamp": int(time.time())
        })]= i
        

    await redis_conn.zadd(
        name=uniswap_pair_cached_block_height_token_price.format(f"{token0}-{token1}"),
        mapping=block_prices
    )
    return len(block_prices)

@provide_async_redis_conn_insta
async def get_price_at_block_height_in_zset(token0, token1, block_number, redis_conn: aioredis.Redis=None):
    
    # get the price at the given block height
    price = await redis_conn.zrangebyscore(
        name=uniswap_pair_cached_block_height_token_price.format(f"{token0}-{token1}"),
        min=block_number,
        max=block_number
    )

    # if price is not found, then return lastest price available for token
    if not price:
        price = await redis_conn.zrevrange(
            name=uniswap_pair_cached_block_height_token_price.format(f"{token0}-{token1}"),
            start=0,
            end=0
        )

    price = json.loads(price[0]) if price else None
    return price


async def extract_recent_transaction_logs(ev_loop, event_name, event_logs, pair_per_token_metadata, token0Price, token1Price):
    """
    Get trade value in USD "for each transaction"
    with amount of each token, txHash and account addresses
    """
    recent_transaction_logs = list()
    for log in event_logs:
        token0_amount = 0
        token1_amount = 0
        trade_amount_usd = 0
        if event_name == 'Swap':
            if log.args.get('amount1In') == 0:
                token0_amount = log.args.get('amount0In')
                token1_amount = log.args.get('amount1Out')
            elif log.args.get('amount0In') == 0:
                token0_amount = log.args.get('amount0Out')
                token1_amount = log.args.get('amount1In')
        elif event_name == 'Mint' or event_name == 'Burn':
            token0_amount = log.args.get('amount0')
            token1_amount = log.args.get('amount1')
        
        # normalize token volume according to decimals specification
        token0_amount = token0_amount / 10 ** int(pair_per_token_metadata['token0']['decimals'])
        token1_amount = token1_amount / 10 ** int(pair_per_token_metadata['token1']['decimals'])

        if event_name == 'Swap':
            if token0Price:
                trade_amount_usd = token0_amount * float(token0Price.decode('utf-8'))
            elif token1Price:
                trade_amount_usd = token1_amount * float(token1Price.decode('utf-8'))
        elif event_name == 'Mint' or event_name == 'Burn':
            if token0Price:
                trade_amount_usd += token0_amount * float(token0Price.decode('utf-8'))
            if token1Price:
                trade_amount_usd += token1_amount * float(token1Price.decode('utf-8'))
            if not token0Price or not token1Price:
                trade_amount_usd *= 2

        block_details = await get_block_details(ev_loop, log["blockNumber"])

        recent_transaction_logs.append({
            "sender": log.args.get("sender", ""),
            "to": log.args.get("to", ""),
            "transactionHash": log["transactionHash"].hex(),
            "logIndex": log["logIndex"],
            "blockNumber": log["blockNumber"],
            "event": log["event"],
            "token0_amount": token0_amount,
            "token1_amount": token1_amount,
            "trade_amount_usd": trade_amount_usd,
            "timestamp": block_details.get("timestamp", "")
        })

    return recent_transaction_logs


async def extract_trade_volume_data(ev_loop, event_name, event_logs: List[AttributeDict], redis_conn: aioredis.Redis, pair_per_token_metadata, from_block):
    log_topic_values = list()
    token0_swapped = 0
    token1_swapped = 0
    trade_volume_token0_usd = 0
    trade_volume_token1_usd = 0
    token0_fee = None
    token1_fee = None
    for log in event_logs:
        log = log.args
        topics = dict()
        for field in uniswap_trade_events_abi[event_name]['inputs']:
            field = field['name']
            topics[field] = log.get(field)
        log_topic_values.append(topics)
    for parsed_log_obj_values in log_topic_values:
        if event_name == 'Swap':
            if parsed_log_obj_values.get('amount1In') == 0:
                token0_swapped += parsed_log_obj_values.get('amount0In')
                token1_swapped += parsed_log_obj_values.get('amount1Out')
                token0_fee = parsed_log_obj_values.get('amount0In')
            elif parsed_log_obj_values.get('amount0In') == 0:
                token0_swapped += parsed_log_obj_values.get('amount0Out')
                token1_swapped += parsed_log_obj_values.get('amount1In')
                token1_fee = parsed_log_obj_values.get('amount1In')
        elif event_name == 'Mint' or event_name == 'Burn':
            token0_swapped += parsed_log_obj_values.get('amount0')
            token1_swapped += parsed_log_obj_values.get('amount1')



    # normalize token volume according to decimals specification
    token0_swapped = token0_swapped / 10 ** int(pair_per_token_metadata['token0']['decimals'])
    token1_swapped = token1_swapped / 10 ** int(pair_per_token_metadata['token1']['decimals'])

    # get conversion
    trade_volume_usd = 0
    trade_fee_usd = 0
    token0Price = await redis_conn.get(
        uniswap_pair_cached_token_price.format(f"{pair_per_token_metadata['token0']['symbol']}-USDT"))
    token1Price = await redis_conn.get(
        uniswap_pair_cached_token_price.format(f"{pair_per_token_metadata['token1']['symbol']}-USDT"))
    
    
    #TODO: instead using to or from block we can make a call for each transaction with its block number
    # but right now whilte storing price in a epoch and not by each block, so does it matter here?
    price_block = int(event_logs[0]['blockNumber']) if event_logs else int(from_block)
    price_pruning_block = price_block - 20 # prune anything older than 20 block from current (each epoch is 10 block rn)
    token0PriceNew, token1PriceNew, *_ = await asyncio.gather(
        get_price_at_block_height_in_zset(pair_per_token_metadata['token0']['symbol'], 'USDT', price_block, redis_conn),
        get_price_at_block_height_in_zset(pair_per_token_metadata['token1']['symbol'], 'USDT', price_block, redis_conn),
        redis_conn.zremrangebyscore(
            uniswap_pair_cached_block_height_token_price.format(f"{pair_per_token_metadata['token0']['symbol']}-USDT"), 
            min='-inf', max=price_pruning_block
        ),
        redis_conn.zremrangebyscore(
            uniswap_pair_cached_block_height_token_price.format(f"{pair_per_token_metadata['token1']['symbol']}-USDT"), 
            min='-inf', max=price_pruning_block
        )
    )
        
    
    #Add Recent Transactions Logs
    recent_transaction_logs = await extract_recent_transaction_logs(ev_loop, event_name, event_logs, pair_per_token_metadata, token0Price, token1Price)


    # if event is 'Swap' then only add single token in total volume calculation
    if event_name == 'Swap':
        # calculate trade volume in USD
        if token0Price:
           trade_volume_usd += token0_swapped * float(token0Price.decode('utf-8'))
        elif token1Price:
           trade_volume_usd += token1_swapped * float(token1Price.decode('utf-8'))

        # calculate uniswap LP fee
        if token0_fee and token0Price:
            token0_fee = token0_fee / 10 ** int(pair_per_token_metadata['token0']['decimals'])
            token0_fee = token0_fee * 0.3 # uniswap LP fee rate
            trade_fee_usd = token0_fee * float(token0Price.decode('utf-8'))
        elif token1_fee and token1Price:
            token1_fee = token1_fee / 10 ** int(pair_per_token_metadata['token1']['decimals'])
            token1_fee = token1_fee * 0.3 # uniswap LP fee rate
            trade_fee_usd = token1_fee * float(token1Price.decode('utf-8'))

        # calculate token trade volume in USD
        trade_volume_token0_usd = token0_swapped * float(token0Price.decode('utf-8')) if token0Price else 0
        trade_volume_token1_usd = token1_swapped * float(token1Price.decode('utf-8')) if token1Price else 0

        return {
            'totalTradesUSD': trade_volume_usd,
            'totalFeeUSD': trade_fee_usd,
            'token0TradeVolume': token0_swapped,
            'token1TradeVolume': token1_swapped,
            'token0TradeVolumeUSD': trade_volume_token0_usd,
            'token1TradeVolumeUSD': trade_volume_token1_usd,
            'recent_transaction_logs': recent_transaction_logs
        }
           
    if token0Price:
        token0Price = float(token0Price.decode('utf-8'))
        trade_volume_usd += token0_swapped * token0Price
        trade_volume_token0_usd = token0_swapped * token0Price
    else:
        logger.warning(
            f"Trade Volume: can't find {pair_per_token_metadata['token0']['symbol']}-"
            f"USDT Price. Attempting to find {pair_per_token_metadata['token1']['symbol']}-USDT price and 2x it"
        )

    if token1Price:
        token1Price = float(token1Price.decode('utf-8'))
        trade_volume_usd += token1_swapped * token1Price
        trade_volume_token1_usd = token1_swapped * token1Price
    else:
        logger.warning(
            f"Trade Volume: can't find {pair_per_token_metadata['token1']['symbol']}-USDT Price")
    
    
    if not token0Price or not token1Price:
        trade_volume_usd *= 2
    
    return {
        'totalTradesUSD': trade_volume_usd,
        'token0TradeVolume': token0_swapped,
        'token1TradeVolume': token1_swapped,
        'token0TradeVolumeUSD': trade_volume_token0_usd,
        'token1TradeVolumeUSD': trade_volume_token1_usd,
        'recent_transaction_logs': recent_transaction_logs
    }


# get allPairLength
def get_all_pair_length():
    return quick_swap_uniswap_v2_factory_contract.functions.allPairsLength().call()


# call allPair by index number
@acquire_threading_semaphore
def get_pair_by_index(index, semaphore=None):
    if not index:
        index = 0
    pair = quick_swap_uniswap_v2_factory_contract.functions.allPairs(index).call()
    return pair


# get list of allPairs using allPairsLength
def get_all_pairs():
    all_pairs = []
    all_pair_length = get_all_pair_length()
    logger.debug(f"All pair length: {all_pair_length}, accumulating all pairs addresses, please wait...")

    # declare semaphore and executor
    sem = threading.BoundedSemaphore(settings.UNISWAP_FUNCTIONS.THREADING_SEMAPHORE)
    with BoundedThreadPoolExecutor(max_workers=settings.UNISWAP_FUNCTIONS.SEMAPHORE_WORKERS) as executor:
        future_to_pairs_addr = {executor.submit(
            get_pair_by_index,
            index=index,
            semaphore=sem
        ): index for index in range(all_pair_length)}
    added = 0
    for future in as_completed(future_to_pairs_addr):
        pair_addr = future_to_pairs_addr[future]
        try:
            rj = future.result()
        except Exception as exc:
            logger.error(f"Error getting address of pair against index: {pair_addr}")
            logger.error(exc, exc_info=True)
            continue
        else:
            if rj:
                all_pairs.append(rj)
                added += 1
                if added % 1000 == 0:
                    logger.debug(f"Accumulated {added} pair addresses")
            else:
                logger.debug(f"Skipping pair address at index: {pair_addr}")
    logger.debug(f"Cached a total {added} pair addresses")
    return all_pairs


# get list of allPairs using allPairsLength and write to file
def get_all_pairs_and_write_to_file():
    try:
        all_pairs = get_all_pairs()
        if not os.path.exists('static/'):
            os.makedirs('static/')

        with open('static/cached_pair_addresses2.json', 'w') as f:
            json.dump(all_pairs, f)
        return all_pairs
    except Exception as e:
        logger.error(e, exc_info=True)
        raise e


@provide_async_redis_conn_insta
async def get_pair_per_token_metadata(pair_contract_obj, pair_address, loop: asyncio.AbstractEventLoop,
                                      redis_conn: aioredis.Redis = None):
    """
        returns information on the tokens contained within a pair contract - name, symbol, decimals of token0 and token1
        also returns pair symbol by concatenating {token0Symbol}-{token1Symbol}
    """
    try:
        pair_address = Web3.toChecksumAddress(pair_address)
        pairTokensAddresses = await redis_conn.hgetall(uniswap_pair_contract_tokens_addresses.format(pair_address))
        if pairTokensAddresses:
            token0Addr = Web3.toChecksumAddress(pairTokensAddresses[b"token0Addr"].decode('utf-8'))
            token1Addr = Web3.toChecksumAddress(pairTokensAddresses[b"token1Addr"].decode('utf-8'))
        else:
            # run in loop's default executor
            pfunc_0 = partial(pair_contract_obj.functions.token0().call)
            token0Addr = await loop.run_in_executor(func=pfunc_0, executor=None)
            pfunc_1 = partial(pair_contract_obj.functions.token1().call)
            token1Addr = await loop.run_in_executor(func=pfunc_1, executor=None)
            token0Addr = Web3.toChecksumAddress(token0Addr)
            token1Addr = Web3.toChecksumAddress(token1Addr)
            await redis_conn.hset(
                name=uniswap_pair_contract_tokens_addresses.format(pair_address),
                mapping={
                    'token0Addr': token0Addr,
                    'token1Addr': token1Addr
                })
        # token0 contract
        token0 = w3.eth.contract(
            address=Web3.toChecksumAddress(token0Addr),
            abi=erc20_abi
        )
        # token1 contract
        token1 = w3.eth.contract(
            address=Web3.toChecksumAddress(token1Addr),
            abi=erc20_abi
        )
        pair_tokens_data = await redis_conn.hgetall(uniswap_pair_contract_tokens_data.format(pair_address))
        if pair_tokens_data:
            token0_decimals = pair_tokens_data[b"token0_decimals"].decode('utf-8')
            token1_decimals = pair_tokens_data[b"token1_decimals"].decode('utf-8')
            token0_symbol = pair_tokens_data[b"token0_symbol"].decode('utf-8')
            token1_symbol = pair_tokens_data[b"token1_symbol"].decode('utf-8')
            token0_name = pair_tokens_data[b"token0_name"].decode('utf-8')
            token1_name = pair_tokens_data[b"token1_name"].decode('utf-8')
        else:
            executor_gather = list()
            executor_gather.append(loop.run_in_executor(func=token0.functions.name().call, executor=None))
            executor_gather.append(loop.run_in_executor(func=token0.functions.symbol().call, executor=None))
            executor_gather.append(loop.run_in_executor(func=token0.functions.decimals().call, executor=None))

            executor_gather.append(loop.run_in_executor(func=token1.functions.name().call, executor=None))
            executor_gather.append(loop.run_in_executor(func=token1.functions.symbol().call, executor=None))
            executor_gather.append(loop.run_in_executor(func=token1.functions.decimals().call, executor=None))

            [
                token0_name, token0_symbol, token0_decimals,
                token1_name, token1_symbol, token1_decimals
            ] = await asyncio.gather(*executor_gather)

            await redis_conn.hset(
                name=uniswap_pair_contract_tokens_data.format(pair_address),
                mapping={
                    "token0_name": token0_name,
                    "token0_symbol": token0_symbol,
                    "token0_decimals": token0_decimals,
                    "token1_name": token1_name,
                    "token1_symbol": token1_symbol,
                    "token1_decimals": token1_decimals,
                    "pair_symbol": f"{token0_symbol}-{token1_symbol}"
                }
            )
            # print(f"pair_symbol {token0_symbol}-{token1_symbol}")
        # TODO: formalize return structure in a pydantic model for better readability
        return {
            'token0': {
                'address': token0Addr,
                'name': token0_name,
                'symbol': token0_symbol,
                'decimals': token0_decimals
            },
            'token1': {
                'address': token1Addr,
                'name': token1_name,
                'symbol': token1_symbol,
                'decimals': token1_decimals
            },
            'pair': {
                'symbol': f'{token0_symbol}-{token1_symbol}'
            }
        }
    except Exception as e:
        # this will be retried in next cycle
        logger.error(f"RPC error while fetcing metadata for pair {pair_address}, error_msg:{e}", exc_info=True)
        return {}


# asynchronously get liquidity of each token reserve
async def get_liquidity_of_each_token_reserve_async(
        loop: asyncio.AbstractEventLoop,
        pair_address,
        redis_conn: aioredis.Redis,
        block_identifier='latest',
        fetch_timestamp=False,
):
    try:
        pair_address = Web3.toChecksumAddress(pair_address)
        # pair contract
        pair = w3.eth.contract(
            address=pair_address,
            abi=pair_contract_abi
        )
        lua_scripts = await load_rate_limiter_scripts(redis_conn)
        logger.debug('Got sha load results for rate limiter scripts: %s', lua_scripts)
        redis_storage = AsyncRedisStorage(lua_scripts, redis_conn)
        custom_limiter = AsyncFixedWindowRateLimiter(redis_storage)
        limit_incr_by = 1  # score to be incremented for each request
        if fetch_timestamp:
            limit_incr_by += 1
        app_id = settings.RPC.MATIC[0].split('/')[
            -1]  # future support for loadbalancing over multiple MaticVigil RPC appID
        key_bits = [app_id, 'eth_call']  # TODO: add unique elements that can identify a request
        can_request = False
        rate_limit_exception = False
        retry_after = 1
        response = None
        for each_lim in PARSED_LIMITS:
            # window_stats = custom_limiter.get_window_stats(each_lim, key_bits)
            # local_app_cacher_logger.debug(window_stats)
            # rest_logger.debug('Limit %s expiry: %s', each_lim, each_lim.get_expiry())
            # async limits rate limit check
            # if rate limit checks out then we call
            try:
                if await custom_limiter.hit(each_lim, limit_incr_by, *[key_bits]) is False:
                    window_stats = await custom_limiter.get_window_stats(each_lim, key_bits)
                    reset_in = 1 + window_stats[0]
                    # if you need information on back offs
                    retry_after = reset_in - int(time.time())
                    retry_after = (datetime.now() + timedelta(0, retry_after)).isoformat()
                    can_request = False
                    break  # make sure to break once false condition is hit
            except (
                    aioredis.exceptions.ConnectionError, aioredis.exceptions.TimeoutError,
                    aioredis.exceptions.ResponseError
            ) as e:
                # shit can happen while each limit check call hits Redis, handle appropriately
                logger.debug('Bypassing rate limit check for appID because of Redis exception: ' + str(
                    {'appID': app_id, 'exception': e}))
            except Exception as e:
                logger.error('Caught exception on rate limiter operations: %s', e, exc_info=True)
                raise
            else:
                can_request = True
        if can_request:
            if fetch_timestamp:
                block_det_func = partial(w3.eth.get_block, block_identifier)
                try:
                    block_details = await loop.run_in_executor(func=block_det_func, executor=None)
                except:
                    block_details = None
            else:
                block_details = None
            pair_per_token_metadata = await get_pair_per_token_metadata(
                pair_contract_obj=pair,
                pair_address=pair_address,
                loop=loop,
                redis_conn=redis_conn
            )
            pfunc_get_reserves = partial(pair.functions.getReserves().call, {'block_identifier': block_identifier})
            async for attempt in AsyncRetrying(reraise=True, stop=stop_after_attempt(3), wait=wait_random(1, 2)):
                with attempt:
                    reserves = await loop.run_in_executor(func=pfunc_get_reserves, executor=None)
                    if reserves:
                        break
            token0_addr = pair_per_token_metadata['token0']['address']
            token1_addr = pair_per_token_metadata['token1']['address']
            token0_decimals = pair_per_token_metadata['token0']['decimals']
            token1_decimals = pair_per_token_metadata['token1']['decimals']
            
            token0Amount = reserves[0] / 10 ** int(token0_decimals)
            token1Amount = reserves[1] / 10 ** int(token1_decimals)
            
            # logger.debug(f"Decimals of token0: {token0_decimals}, Decimals of token1: {token1_decimals}")
            logger.debug("Token0: %s, Reserves: %s | Token1: %s, Reserves: %s", token0_addr, token1_addr, token0Amount, token1Amount)
                
            token0Price, token1Price = await redis_conn.mget([
                uniswap_pair_cached_token_price.format(f"{pair_per_token_metadata['token0']['symbol']}-USDT"),
                uniswap_pair_cached_token_price.format(f"{pair_per_token_metadata['token1']['symbol']}-USDT")
            ])

            token0USD = 0
            token1USD = 0
            if token0Price:
                token0USD = token0Amount * float(token0Price.decode('utf-8'))
            else:
                logger.error(f"Liquidity: Could not find token0 price for {pair_per_token_metadata['token0']['symbol']}-USDT, setting it to 0")
            
            if token1Price:
                token1USD = token1Amount * float(token1Price.decode('utf-8'))
            else:
                logger.error(f"Liquidity: Could not find token1 price for {pair_per_token_metadata['token1']['symbol']}-USDT, setting it to 0")
                

            return {
                'token0': token0Amount,
                'token1': token1Amount,
                'token0USD': token0USD,
                'token1USD': token1USD,
                'timestamp': None if not block_details else block_details.timestamp
            }
        else:
            raise Exception("exhausted_api_key_rate_limit inside uniswap_functions get async liquidity reservers")
    except Exception as exc:
        logger.error("error at async_get_liquidity_of_each_token_reserve fn: %s", exc, exc_info=True)
        # snapshot constructor expect exception and handle it with queue
        raise exc


# asynchronously get trades on a pair contract
@provide_async_redis_conn_insta
async def get_pair_contract_trades_async(
        ev_loop: asyncio.AbstractEventLoop,
        pair_address,
        from_block,
        to_block,
        fetch_timestamp=True,
        redis_conn: aioredis.Redis = None
):
    try:
        pair_address = Web3.toChecksumAddress(pair_address)
        # pair contract
        pair = w3.eth.contract(
            address=pair_address,
            abi=pair_contract_abi
        )
        redis_storage = AsyncRedisStorage(await load_rate_limiter_scripts(redis_conn), redis_conn)
        custom_limiter = AsyncFixedWindowRateLimiter(redis_storage)
        limit_incr_by = 3  # be honest, we will make 3 eth_getLogs queries here
        app_id = settings.RPC.MATIC[0].split('/')[
            -1]  # future support for loadbalancing over multiple MaticVigil RPC appID
        key_bits = [app_id, 'eth_logs']  # TODO: add unique elements that can identify a request
        can_request = False
        rate_limit_exception = False
        retry_after = 1
        response = None
        for each_lim in PARSED_LIMITS:
            # window_stats = custom_limiter.get_window_stats(each_lim, key_bits)
            # local_app_cacher_logger.debug(window_stats)
            # rest_logger.debug('Limit %s expiry: %s', each_lim, each_lim.get_expiry())
            # async limits rate limit check
            # if rate limit checks out then we call
            try:
                if await custom_limiter.hit(each_lim, limit_incr_by, *[key_bits]) is False:
                    window_stats = await custom_limiter.get_window_stats(each_lim, key_bits)
                    reset_in = 1 + window_stats[0]
                    # if you need information on back offs
                    retry_after = reset_in - int(time.time())
                    retry_after = (datetime.now() + timedelta(0, retry_after)).isoformat()
                    can_request = False
                    break  # make sure to break once false condition is hit
            except (
                    aioredis.errors.ConnectionClosedError, aioredis.errors.ConnectionForcedCloseError,
                    aioredis.errors.PoolClosedError
            ) as e:
                # shit can happen while each limit check call hits Redis, handle appropriately
                logger.debug('Bypassing rate limit check for appID because of Redis exception: ' + str(
                    {'appID': app_id, 'exception': e}))
            else:
                can_request = True
        if can_request:
            if fetch_timestamp:
                # logger.debug('Attempting to get block details of to_block %s', to_block)
                block_det_func = partial(w3.eth.get_block, to_block)
                try:
                    block_details = await ev_loop.run_in_executor(func=block_det_func, executor=None)
                except Exception as e:
                    logger.error('Error attempting to get block details of to_block %s: %s', to_block, e, exc_info=True)
                    block_details = None
            else:
                # logger.debug('Not attempting to get block details of to_block %s', to_block)
                block_details = None
            pair_per_token_metadata = await get_pair_per_token_metadata(
                pair_contract_obj=pair,
                pair_address=pair_address,
                loop=ev_loop
            )
            event_log_fetch_coros = list()
            for trade_event_name in ['Swap', 'Mint', 'Burn']:
                event_sig, event_abi = get_event_sig_and_abi(trade_event_name)
                pfunc_get_event_logs = partial(
                    get_events_logs, **{
                        'contract_address': pair_address,
                        'toBlock': to_block,
                        'fromBlock': from_block,
                        'topics': [event_sig],
                        'event_abi': event_abi
                    }
                )
                event_log_fetch_coros.append(ev_loop.run_in_executor(func=pfunc_get_event_logs, executor=None))
            [
                swap_event_logs, mint_event_logs, burn_event_logs
            ] = await asyncio.gather(*event_log_fetch_coros)
            logs_ret = {
                'Swap': swap_event_logs,
                'Mint': mint_event_logs,
                'Burn': burn_event_logs
            }
            # extract total trade from them
            rets = dict()
            for trade_event_name in ['Swap', 'Mint', 'Burn']:
                # print(f'Event {trade_event_name} logs: ', logs_ret[trade_event_name])
                rets.update({
                    trade_event_name: {
                        'logs': [{
                            **dict(k.args),
                            "transactionHash": k["transactionHash"].hex(),
                            "logIndex": k["logIndex"],
                            "blockNumber": k["blockNumber"],
                            "event": k["event"]
                        } for k in logs_ret[trade_event_name]],
                        'trades': await extract_trade_volume_data(
                            ev_loop=ev_loop,
                            event_name=trade_event_name,
                            # event_logs=logs_ret[trade_event_name],
                            event_logs=logs_ret[trade_event_name],
                            redis_conn=redis_conn,
                            pair_per_token_metadata=pair_per_token_metadata,
                            from_block=from_block
                        )
                    }
                })
            max_block_timestamp = None if not block_details else block_details.timestamp
            rets.update({'timestamp': max_block_timestamp})
            return rets
        else:
            raise Exception("exhausted_api_key_rate_limit inside uniswap_functions get async liquidity reservers")
    except Exception as exc:
        logger.error("error at get_pair_contract_trades_async fn: %s", exc, exc_info=True)
        # snapshot constructor expect exception and handle it with queue
        raise exc


# get liquidity of each token reserve
def get_liquidity_of_each_token_reserve(pair_address, block_identifier='latest'):
    # logger.debug("Pair Data:")
    pair_address = Web3.toChecksumAddress(pair_address)
    # pair contract
    pair = w3.eth.contract(
        address=pair_address,
        abi=pair_contract_abi
    )

    token0Addr = pair.functions.token0().call()
    token1Addr = pair.functions.token1().call()
    # async limits rate limit check
    # if rate limit checks out then we call
    # introduce block height in get reserves
    reservers = pair.functions.getReserves().call(block_identifier=block_identifier)
    logger.debug(f"Token0: {token0Addr}, Reservers: {reservers[0]}")
    logger.debug(f"Token1: {token1Addr}, Reservers: {reservers[1]}")

    # toke0 contract
    token0 = w3.eth.contract(
        address=Web3.toChecksumAddress(token0Addr),
        abi=erc20_abi
    )
    # toke1 contract
    token1 = w3.eth.contract(
        address=Web3.toChecksumAddress(token1Addr),
        abi=erc20_abi
    )

    token0_decimals = token0.functions.decimals().call()
    token1_decimals = token1.functions.decimals().call()

    logger.debug(f"Decimals of token1: {token1_decimals}, Decimals of token1: {token0_decimals}")
    logger.debug(
        f"reservers[0]/10**token0_decimals: {reservers[0] / 10 ** token0_decimals}, reservers[1]/10**token1_decimals: {reservers[1] / 10 ** token1_decimals}")

    return {"token0": reservers[0] / 10 ** token0_decimals, "token1": reservers[1] / 10 ** token1_decimals}


def get_pair(token0, token1):
    token0 = w3.toChecksumAddress(token0)
    token1 = w3.toChecksumAddress(token1)
    pair = quick_swap_uniswap_v2_factory_contract.functions.getPair(token0, token1).call()
    return pair


async def get_aiohttp_cache() -> aiohttp.ClientSession:
    basic_rpc_connector = aiohttp.TCPConnector(limit=settings['rlimit']['file_descriptors'])
    aiohttp_client_basic_rpc_session = aiohttp.ClientSession(connector=basic_rpc_connector)
    return aiohttp_client_basic_rpc_session

if __name__ == '__main__':
    # here instead of calling get pair we can directly use cached all pair addresses
    # dai = "0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063"
    # gns = "0xE5417Af564e4bFDA1c483642db72007871397896"
    # weth = "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619"
    # pair_address = get_pair("0x29bf8Df7c9a005a080E4599389Bf11f15f6afA6A", "0xc2132d05d31c914a87c6611c10748aeb04b58e8f")
    # print(f"pair_address: {pair_address}")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        get_pair_contract_trades_async(loop, '0x5fa464cefe8901d66c09b85d5fcdc55b3738c688', 14412884, 14428003)
    )

    # logger.debug(f"Pair address : {pair_address}")
    # logger.debug(get_liquidity_of_each_token_reserve(pair_address))

    # # #we can pass block_identifier=chain_height
    # # print(get_liquidity_of_each_token_reserve(pair_address, block_identifier=24265790))

    # # async liqudity function
    # reservers = loop.run_until_complete(async_get_liquidity_of_each_token_reserve(loop, pair_address="0x9d3cd87FFEB9eBa14F63DeC135Da5153eC5CA698"))
    # loop.close()
