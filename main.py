# main.py 主逻辑：包括字段拼接、模拟请求
import json
import time
import random
import logging
import hashlib
import requests
import urllib.parse
from push import push
from log_utils import setup_logging
from config import data, headers, cookies, READ_NUM, PUSH_METHOD, PUSH_NOTIFY_TYPE, book, chapter


# 加密盐及其它默认值
KEY = "3c5c8717f3daf09iop3423zafeqoi"
READ_URL = "https://weread.qq.com/web/book/read"
RENEW_URL = "https://weread.qq.com/web/login/renewal"
FIX_SYNCKEY_URL = "https://weread.qq.com/web/book/chapterInfos"
COOKIE_DATA_VARIANTS = [{"rq": "%2Fweb%2Fbook%2Fread", "ql": False},{"rq": "%2Fweb%2Fbook%2Fread", "ql": True},{"rq": "%2Fweb%2Fbook%2Fread"},]
REQUEST_TIMEOUT = (10, 30)
SYNCKEY_REPAIR_LIMIT = 3
SYNCKEY_REPAIR_DELAY = 5


def encode_data(data):
    """数据编码"""
    return '&'.join(f"{k}={urllib.parse.quote(str(data[k]), safe='')}" for k in sorted(data.keys()))


def cal_hash(input_string):
    """计算哈希值"""
    _7032f5 = 0x15051505
    _cc1055 = _7032f5
    length = len(input_string)
    _19094e = length - 1

    while _19094e > 0:
        _7032f5 = 0x7fffffff & (_7032f5 ^ ord(input_string[_19094e]) << (length - _19094e) % 30)
        _cc1055 = 0x7fffffff & (_cc1055 ^ ord(input_string[_19094e - 1]) << _19094e % 30)
        _19094e -= 2

    return hex(_7032f5 + _cc1055)[2:].lower()

def get_wr_skey():
    """刷新cookie密钥"""
    for cookie_data in COOKIE_DATA_VARIANTS:
        try:
            response = requests.post(
                RENEW_URL, headers=headers, cookies=cookies,
                data=json.dumps(cookie_data, separators=(',', ':')),
                timeout=REQUEST_TIMEOUT,
            )
            
            if 'wr_skey' in response.cookies:
                return response.cookies['wr_skey'][:8]
            else:
                continue
        except requests.RequestException as exc:
            logging.warning(f"refresh_cookie 请求失败，payload={cookie_data}，原因：{exc}")
            continue
        
        
    return None

def fix_no_synckey():
    response = requests.post(
        FIX_SYNCKEY_URL, headers=headers, cookies=cookies,
        data=json.dumps({"bookIds": ["3300060341"]}, separators=(',', ':')),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()


def read_response():
    try:
        response = requests.post(
            READ_URL, headers=headers, cookies=cookies,
            data=json.dumps(data, separators=(',', ':')),
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"阅读请求失败（{type(exc).__name__}）。") from None

    try:
        result = response.json()
    except ValueError:
        raise RuntimeError(f"阅读接口返回非 JSON 响应（HTTP {response.status_code}）。") from None

    if not isinstance(result, dict):
        raise RuntimeError(f"阅读接口未返回 JSON 对象（HTTP {response.status_code}）。")

    err_code = result.get('errCode')
    diagnostic = f"HTTP {response.status_code}"
    if type(err_code) is int:
        diagnostic += f"，errCode={err_code}"
    if not response.ok:
        raise RuntimeError(f"阅读接口请求失败（{diagnostic}）。")
    if 'succ' not in result:
        logging.warning("阅读接口返回异常（%s），尝试刷新 cookie。", diagnostic)
    return result

refresh_print = setup_logging()

def refresh_cookie():
    logging.info("刷新 cookie")
    new_skey = get_wr_skey()
    if new_skey:
        cookies['wr_skey'] = new_skey
        logging.info(f"密钥刷新成功，新密钥：{new_skey[:2]}***")
        logging.info("重新本次阅读。")
    else:
        ERROR_CODE = "无法获取新密钥或者 WXREAD_CURL_BASH 配置有误，终止运行。"
        logging.error(ERROR_CODE)
        raise Exception(ERROR_CODE)


def should_push_success():
    notify_type = str(PUSH_NOTIFY_TYPE or "all").strip().lower()
    return notify_type not in ("fail", "failed", "failure", "fail_only", "failure_only", "only_fail")


def run_read_task():
    refresh_cookie()
    index = 1
    lastTime = int(time.time()) - 30
    synckey_repair_attempts = 0
    logging.info(f"一共需要阅读 {READ_NUM} 次。")

    while index <= READ_NUM:
        data.pop('s')
        data['b'] = random.choice(book)
        data['c'] = random.choice(chapter)
        thisTime = int(time.time())
        data['ct'] = thisTime
        data['rt'] = thisTime - lastTime
        data['ts'] = int(thisTime * 1000) + random.randint(0, 1000)
        data['rn'] = random.randint(0, 1000)
        data['sg'] = hashlib.sha256(f"{data['ts']}{data['rn']}{KEY}".encode()).hexdigest()
        data['s'] = cal_hash(encode_data(data))

        refresh_print(f"阅读进度: 第 {index}/{READ_NUM} 次，已完成 {(index - 1) * 0.5:.1f} 分钟")
        logging.debug("data: %s", data)
        resData = read_response()
        logging.debug("response: %s", resData)

        if 'succ' in resData:
            if 'synckey' in resData:
                synckey_repair_attempts = 0
                lastTime = thisTime
                index += 1
                time.sleep(30)
                refresh_print(f"阅读进度: 第 {min(index, READ_NUM + 1) - 1}/{READ_NUM} 次，已完成 {(index - 1) * 0.5:.1f} 分钟")
            else:
                if synckey_repair_attempts >= SYNCKEY_REPAIR_LIMIT:
                    raise RuntimeError(f"连续 {SYNCKEY_REPAIR_LIMIT} 次未恢复阅读进度同步，终止运行。")
                synckey_repair_attempts += 1
                logging.warning(
                    "无 synckey，尝试修复（%d/%d）...",
                    synckey_repair_attempts, SYNCKEY_REPAIR_LIMIT,
                )
                try:
                    fix_no_synckey()
                except requests.HTTPError as exc:
                    raise RuntimeError(f"阅读进度同步修复失败（HTTP {exc.response.status_code}）。") from None
                except requests.RequestException as exc:
                    raise RuntimeError(f"阅读进度同步修复请求失败（{type(exc).__name__}）。") from None
                time.sleep(SYNCKEY_REPAIR_DELAY)
        else:
            refresh_cookie()

    logging.info("阅读脚本已完成。")
    return (index - 1) * 0.5


def main():
    try:
        read_minutes = run_read_task()
    except Exception as exc:
        error_code = f"微信读书自动阅读失败：{exc}"
        logging.exception(error_code)
        push(error_code, PUSH_METHOD, is_success=False)
        raise

    if PUSH_METHOD not in (None, ''):
        if should_push_success():
            logging.info("开始推送...")
            push(f"微信读书自动阅读完成。\n阅读时长：{read_minutes} 分钟。", PUSH_METHOD, is_success=True)
        else:
            logging.info("当前配置为仅失败时推送，跳过成功推送。")
    else:
        logging.info("未配置推送渠道，跳过推送。")


if __name__ == "__main__":
    main()
