import json
import logging.handlers
import sys
from functools import wraps

import aiohttp
import requests

from pooler.settings.config import settings


def make_post_call(url: str, params: dict):
    try:
        logging.debug('Making post call to {}: {}', url, params)
        response = requests.post(url, json=params)
        if response.status_code == 200:
            return response.json()
        else:
            msg = f'Failed to make request {params}. Got status response from {url}: {response.status_code}'
            return None
    except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ReadTimeout,
            requests.exceptions.RequestException,
            requests.exceptions.ConnectionError,
    ) as terr:
        logging.debug('Error occurred while making the post call.')
        logging.error(terr, exc_info=True)
        return None


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
            'exception': None,
        }
        if isinstance(self.underlying_exception, Exception):
            ret.update({'exception': self.underlying_exception.__str__()})
        return json.dumps(ret)

    def __repr__(self):
        return self.__str__()


# TODO: support basic failover and/or load balanced calls that use the list of URLs. Introduce in rpc_helper.py
async def make_post_call_async(url: str, params: dict, session: aiohttp.ClientSession, tag: int):
    try:
        message = f'Making async post call to {url}: {params}'
        logging.debug(message)
        response_status_code = None
        response = None
        # per request timeout instead of configuring a client session wide timeout
        # from reported issue https://github.com/aio-libs/aiohttp/issues/3203
        async with session.post(
            url=url, json=params, timeout=aiohttp.ClientTimeout(
                total=None,
                sock_read=settings.timeouts.archival,
                sock_connect=settings.timeouts.connection_init,
            ),
        ) as response_obj:
            response = await response_obj.json()
            response_status_code = response_obj.status
        if response_status_code == 200 and type(response) is dict:
            response.update({'tag': tag})
            return response
        else:
            msg = f'Failed to make request {params}. Got status response from {url}: {response_status_code}'
            logging.error(msg)
            raise RPCException(
                request=params, response=response, underlying_exception=None,
                extra_info={'msg': msg, 'tag': tag},
            )
    except aiohttp.ClientResponseError as terr:
        msg = 'aiohttp error occurred while making async post call'
        logging.debug(msg)
        logging.error(terr, exc_info=True)
        raise RPCException(
            request=params, response=response, underlying_exception=terr,
            extra_info={'msg': msg, 'tag': tag},
        )
    except Exception as e:
        msg = 'Exception occurred while making async post call'
        logging.debug(msg)
        logging.error(e, exc_info=True)
        raise RPCException(
            request=params, response=response, underlying_exception=e,
            extra_info={'msg': msg, 'tag': tag},
        )


def cleanup_children_procs(fn):
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            fn(self, *args, **kwargs)
            logging.info('Finished running process hub core...')
        except Exception as e:
            logging.opt(exception=True).error(
                'Received an exception on process hub core run(): {}',
                e,
            )
            # logging.error('Initiating kill children....')
            # # silently kill all children
            # procs = psutil.Process().children()
            # for p in procs:
            #     p.terminate()
            # gone, alive = psutil.wait_procs(procs, timeout=3)
            # for p in alive:
            #     logging.error(f'killing process: {p.name()}')
            #     p.kill()
            logging.error('Waiting on spawned callback workers to join...')
            for worker_class_name, unique_worker_entries in self._spawned_cb_processes_map.items():
                for worker_unique_id, worker_unique_process_details in unique_worker_entries.items():
                    if worker_unique_process_details['process'].pid:
                        logging.error(
                            'Waiting on spawned callback worker {} | Unique ID {} | PID {}  to join...',
                            worker_class_name, worker_unique_id, worker_unique_process_details['process'].pid,
                        )
                        worker_unique_process_details['process'].join()

            logging.error(
                'Waiting on spawned core workers to join... {}',
                self._spawned_processes_map,
            )
            for worker_class_name, unique_worker_entries in self._spawned_processes_map.items():
                logging.error('spawned Process Pid to wait on {}', unique_worker_entries.pid)
                # internal state reporter might set proc_id_map[k] = -1
                if unique_worker_entries != -1:
                    logging.error(
                        'Waiting on spawned core worker {} | PID {}  to join...',
                        worker_class_name, unique_worker_entries.pid,
                    )
                    unique_worker_entries.join()
            logging.error('Finished waiting for all children...now can exit.')
        finally:
            logging.error('Finished waiting for all children...now can exit.')
            self._reporter_thread.join()
            sys.exit(0)
            # sys.exit(0)
    return wrapper


def acquire_threading_semaphore(fn):
    @wraps(fn)
    def semaphore_wrapper(*args, **kwargs):
        semaphore = kwargs['semaphore']

        logging.debug('Acquiring threading semaphore')
        semaphore.acquire()
        try:
            resp = fn(*args, **kwargs)
        except Exception:
            raise
        finally:
            semaphore.release()

        return resp

    return semaphore_wrapper


# # # END: placeholder for supporting liquidity events