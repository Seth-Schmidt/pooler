from pooler.settings.config import settings

uniswap_pair_contract_tokens_addresses = (
    'uniswap:pairContract:' + settings.namespace + ':{}:PairContractTokensAddresses'
)
uniswap_pair_contract_tokens_data = (
    'uniswap:pairContract:' + settings.namespace + ':{}:PairContractTokensData'
)

uniswap_V2_summarized_snapshots_zset = (
    'uniswap:V2PairsSummarySnapshot:' + settings.namespace + ':snapshotsZset'
)
uniswap_V2_snapshot_at_blockheight = (
    'uniswap:V2PairsSummarySnapshot:' + settings.namespace + ':snapshot:{}'
)  # block_height
uniswap_v2_daily_stats_snapshot_zset = (
    'uniswap:V2DailyStatsSnapshot:' + settings.namespace + ':snapshotsZset'
)
uniswap_V2_daily_stats_at_blockheight = (
    'uniswap:V2DailyStatsSnapshot:' + settings.namespace + ':snapshot:{}'
)  # block_height
uniswap_v2_tokens_snapshot_zset = (
    'uniswap:V2TokensSummarySnapshot:' + settings.namespace + ':snapshotsZset'
)
uniswap_V2_tokens_at_blockheight = (
    'uniswap:V2TokensSummarySnapshot:' + settings.namespace + ':{}'
)  # block_height

uniswap_pair_cached_recent_logs = (
    'uniswap:pairContract:' + settings.namespace + ':{}:recentLogs'
)

uniswap_eth_usd_price_zset = (
    'uniswap:ethBlockHeightPrice:' + settings.namespace + ':ethPriceZset'
)
uniswap_tokens_pair_map = (
    'uniswap:pairContract:' + settings.namespace + ':tokensPairMap'
)

uniswap_pair_cached_block_height_token_price = (
    'uniswap:pairContract:' + settings.namespace +
    ':{}:cachedPairBlockHeightTokenPrice'
)
