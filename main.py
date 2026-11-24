# main.py 主逻辑：包括字段拼接、模拟请求
import json
import time
import random
import logging
import hashlib
import os
import requests
import urllib.parse
from datetime import datetime, time as clock_time, timedelta, timezone
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
BEIJING_TZ = timezone(timedelta(hours=8))
STATS_URL = "https://i.weread.qq.com/api/agent/gateway"


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


def fetch_read_seconds(today):
    api_key = os.getenv("WEREAD_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("动态模式需要配置微信读书官方 API Key（WEREAD_API_KEY）。")

    day_start = datetime.combine(today, clock_time.min, BEIJING_TZ)
    try:
        response = requests.post(
            STATS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "api_name": "/readdata/detail",
                "mode": "monthly",
                "baseTime": int((day_start + timedelta(hours=12)).timestamp()),
                "skill_version": "1.0.6",
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"阅读统计请求失败（{type(exc).__name__}）。") from None
    if response.status_code != 200:
        raise RuntimeError(f"阅读统计请求失败（HTTP {response.status_code}）。")
    try:
        result = response.json()
    except ValueError:
        raise RuntimeError("阅读统计接口返回了非 JSON 响应。") from None
    if not isinstance(result, dict) or result.get("errcode", 0) != 0:
        raise RuntimeError("微信读书未返回有效的阅读统计，请检查官方 API Key。")

    daily_totals = result.get("readTimes")
    if not isinstance(daily_totals, dict):
        raise RuntimeError("微信读书未返回每日阅读明细，无法判断今天的时长。")
    if any(not timestamp.isascii() or not timestamp.isdecimal() or len(timestamp) > 12
           or (int(timestamp) - int(day_start.timestamp())) % 86400 for timestamp in daily_totals):
        raise RuntimeError("微信读书返回的每日统计时间与北京时间不匹配。")
    seconds = daily_totals.get(str(int(day_start.timestamp())), 0)
    if type(seconds) is not int or not 0 <= seconds <= 86400:
        raise RuntimeError("微信读书返回的当天阅读时长无效。")
    return seconds


def select_read_target(today):
    mode = os.getenv("READ_MODE", "fixed")
    if mode == "fixed":
        return READ_NUM
    if mode != "adaptive":
        raise ValueError("READ_MODE 必须为 adaptive 或 fixed。")

    if os.getenv("GITHUB_ACTIONS") == "true" and os.getenv("READING_DAY") != today.isoformat():
        raise RuntimeError("当天运行记录未建立或已跨日，跳过本次动态任务。")
    seconds = fetch_read_seconds(today)
    if os.getenv("MIDDAY_STARTED") == "true":
        seconds = max(0, seconds - 300)
    if seconds == 0:
        target = 10  # 当天没自己看书：象征性刷 5 分钟（10 次 × 30 秒）
    else:
        target = min(180, 30 + (seconds ** 2 + 107999) // 108000)
    # 封顶到零点：每次阅读约 33 秒（含请求耗时），预留 45 秒缓冲，保证当天内刷完不跨天
    now = datetime.now(BEIJING_TZ)
    midnight = datetime.combine(today + timedelta(days=1), clock_time.min, BEIJING_TZ)
    max_reads_by_time = max(0, int(((midnight - now).total_seconds() - 45) // 33))
    return min(target, max_reads_by_time)


def run_read_task():
    today = datetime.now(BEIJING_TZ).date()
    read_target = select_read_target(today)
    refresh_cookie()
    index = 1
    lastTime = int(time.time()) - 30
    synckey_repair_attempts = 0
    logging.info(f"一共需要阅读 {read_target} 次。")

    while index <= read_target:
        if datetime.now(BEIJING_TZ).date() != today:
            logging.info("已跨过零点，停止本次阅读，剩余次数作废。")
            break
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

        refresh_print(f"阅读进度: 第 {index}/{read_target} 次，已完成 {(index - 1) * 0.5:.1f} 分钟")
        logging.debug("data: %s", data)
        resData = read_response()
        logging.debug("response: %s", resData)

        if 'succ' in resData:
            if 'synckey' in resData:
                synckey_repair_attempts = 0
                lastTime = thisTime
                index += 1
                time.sleep(30)
                refresh_print(f"阅读进度: 第 {index - 1}/{read_target} 次，已完成 {(index - 1) * 0.5:.1f} 分钟")
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
