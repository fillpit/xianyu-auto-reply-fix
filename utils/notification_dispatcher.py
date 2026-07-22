import asyncio
import json
import threading
from typing import Any, Dict, Iterable, Optional

import aiohttp
from loguru import logger


SUPPORTED_NOTIFICATION_TEMPLATE_TYPES = (
    'message',
    'token_refresh',
    'delivery',
    'slider_success',
    'face_verify',
    'password_login_success',
    'cookie_refresh_success',
)


DEFAULT_NOTIFICATION_TEMPLATES = {
    'message': '''🚨 接收消息通知

账号: {account_id}
买家: {buyer_name} (ID: {buyer_id})
商品ID: {item_id}
聊天ID: {chat_id}
消息内容: {message}

时间: {time}''',
    'token_refresh': '''Token刷新异常

账号ID: {account_id}
异常时间: {time}
异常信息: {error_message}

请检查账号Cookie是否过期，如有需要请及时更新Cookie配置。''',
    'delivery': '''🚨 自动发货通知

账号: {account_id}
买家: {buyer_name} (ID: {buyer_id})
商品ID: {item_id}
聊天ID: {chat_id}
结果: {result}
时间: {time}

请及时处理！''',
    'slider_success': '''✅ 滑块验证成功，{status_text}

账号: {account_id}
时间: {time}''',
    'face_verify': '''⚠️ 需要{verification_type} 🚫
在验证期间，发货及自动回复暂时无法使用。

{verification_action}
{verification_url}

账号: {account_id}
时间: {time}''',
    'password_login_success': '''✅ 密码登录成功

账号: {account_id}
时间: {time}
Cookie数量: {cookie_count}

账号Cookie已更新，正在重启服务...''',
    'cookie_refresh_success': '''✅ 刷新Cookie成功

账号: {account_id}
时间: {time}
Cookie数量: {cookie_count}

账号已可正常使用。''',
}


VERIFICATION_TYPE_LABELS = {
    'face_verify': '人脸验证',
    'sms_verify': '短信验证',
    'qr_verify': '二维码验证',
    'unknown': '身份验证',
}


def _safe_str(value: Any) -> str:
    try:
        return str(value)
    except Exception:
        return repr(value)


def normalize_channel_type(channel_type: Any) -> str:
    normalized = str(channel_type or '').strip().lower()
    mapping = {
        'bark': 'bark',
    }
    return mapping.get(normalized, normalized)


def parse_notification_config(config: Any) -> Dict[str, Any]:
    if isinstance(config, dict):
        return dict(config)

    try:
        if isinstance(config, str):
            return json.loads(config)
    except (json.JSONDecodeError, TypeError):
        pass

    return {'config': config}


def get_notification_template_text(template_type: str) -> str:
    from db_manager import db_manager

    try:
        template_data = db_manager.get_notification_template(template_type)
        if template_data and template_data.get('template'):
            return template_data['template']
    except Exception as exc:
        logger.warning(f"获取通知模板失败: {_safe_str(exc)}")

    return DEFAULT_NOTIFICATION_TEMPLATES.get(template_type, '')


def format_notification_template(template: str, **kwargs: Any) -> str:
    rendered = template or ''
    try:
        for key, value in kwargs.items():
            rendered = rendered.replace(f'{{{key}}}', str(value) if value is not None else '未知')
        return rendered
    except Exception as exc:
        logger.error(f"格式化模板失败: {_safe_str(exc)}")
        return rendered


def render_notification_template(template_type: str, **kwargs: Any) -> str:
    template = get_notification_template_text(template_type)
    return format_notification_template(template, **kwargs)


def guess_verification_type(error_message: str = '', verification_url: str = '') -> str:
    text = f"{error_message or ''} {verification_url or ''}"
    if '人脸' in text:
        return '人脸验证'
    if '短信' in text:
        return '短信验证'
    if '二维码' in text or '扫码' in text:
        return '二维码验证'
    return '身份验证'


def resolve_verification_type_label(
    verification_type: str = '',
    error_message: str = '',
    verification_url: str = '',
) -> str:
    normalized = str(verification_type or '').strip()
    if normalized in VERIFICATION_TYPE_LABELS:
        return VERIFICATION_TYPE_LABELS[normalized]
    if normalized in VERIFICATION_TYPE_LABELS.values():
        return normalized
    return guess_verification_type(error_message, verification_url)


def build_face_verify_notification(
    account_id: str,
    time_text: str,
    *,
    verification_type: str = '',
    verification_url: str = '',
    error_message: str = '',
    has_screenshot: bool = False,
) -> str:
    verification_type_label = resolve_verification_type_label(
        verification_type,
        error_message,
        verification_url,
    )

    if has_screenshot:
        verification_action = '请在自动化网站的账号管理弹窗中扫描二维码完成验证:'
        verification_target = '自动化网站账号管理弹窗中的验证二维码'
    else:
        verification_action = '请点击验证链接完成验证:'
        verification_target = verification_url or '无'

    return render_notification_template(
        'face_verify',
        account_id=account_id,
        time=time_text,
        verification_action=verification_action,
        verification_url=verification_target,
        verification_type=verification_type_label,
    )


async def _send_bark_notification(config_data: Dict[str, Any], message: str, *, title: str, account_id: str = '') -> bool:
    server_url = str(config_data.get('server_url', 'https://api.day.app') or 'https://api.day.app').rstrip('/')
    device_key = config_data.get('device_key', '')
    if not device_key:
        logger.warning(f"【{account_id}】Bark通知未配置设备密钥")
        return False

    data = {
        'device_key': device_key,
        'title': config_data.get('title') or title,
        'body': message,
        'sound': config_data.get('sound', 'default'),
        'group': config_data.get('group', 'xianyu'),
    }
    if config_data.get('icon'):
        data['icon'] = config_data['icon']
    if config_data.get('url'):
        data['url'] = config_data['url']

    async with aiohttp.ClientSession() as session:
        async with session.post(f'{server_url}/push', json=data, timeout=10) as response:
            response_text = await response.text()
            if response.status != 200:
                logger.warning(f"【{account_id}】Bark通知发送失败: HTTP {response.status}, 响应: {response_text}")
                return False
            try:
                payload = json.loads(response_text)
                if payload.get('code') != 200:
                    logger.warning(f"【{account_id}】Bark通知发送失败: {payload.get('message', '未知错误')}")
                    return False
            except json.JSONDecodeError:
                if 'success' not in response_text.lower() and 'ok' not in response_text.lower():
                    logger.warning(f"【{account_id}】Bark通知响应格式异常: {response_text}")
                    return False
            logger.info(f"【{account_id}】Bark通知发送成功")
            return True


async def send_channel_notification(channel_type: Any, config_data: Dict[str, Any], message: str, *, title: str = '闲鱼管理系统通知', notification_type: str = 'info', attachment_path: Optional[str] = None, account_id: str = '') -> bool:
    normalized_type = normalize_channel_type(channel_type)
    if normalized_type == 'bark':
        return await _send_bark_notification(config_data, message, title=title, account_id=account_id)

    logger.warning(f"【{account_id}】不支持的通知渠道类型: {channel_type}")
    return False


async def dispatch_notifications(notifications: Iterable[Dict[str, Any]], message: str, *, title: str = '闲鱼管理系统通知', notification_type: str = 'info', attachment_path: Optional[str] = None, account_id: str = '') -> bool:
    notification_sent = False

    for notification in notifications or []:
        if not notification.get('enabled', True):
            continue

        channel_type = notification.get('channel_type') or notification.get('type')
        channel_name = notification.get('channel_name') or notification.get('name') or str(channel_type or 'unknown')
        channel_config = notification.get('channel_config') if 'channel_config' in notification else notification.get('config')
        try:
            config_data = parse_notification_config(channel_config)
            channel_sent = await send_channel_notification(
                channel_type,
                config_data,
                message,
                title=title,
                notification_type=notification_type,
                attachment_path=attachment_path,
                account_id=account_id,
            )
            if channel_sent:
                notification_sent = True
        except Exception as exc:
            logger.error(f"【{account_id}】发送通知失败 ({channel_name}): {_safe_str(exc)}")

    return notification_sent


async def dispatch_account_notifications(account_id: str, message: str, *, title: str = '闲鱼管理系统通知', notification_type: str = 'info', attachment_path: Optional[str] = None) -> bool:
    from db_manager import db_manager

    try:
        notifications = db_manager.get_account_notifications(account_id)
    except Exception as exc:
        logger.warning(f"【{account_id}】获取通知配置失败: {_safe_str(exc)}")
        return False

    if not notifications:
        logger.warning(f"【{account_id}】未配置消息通知，跳过发送")
        return False

    return await dispatch_notifications(
        notifications,
        message,
        title=title,
        notification_type=notification_type,
        attachment_path=attachment_path,
        account_id=account_id,
    )


def dispatch_account_notifications_sync(account_id: str, message: str, *, title: str = '闲鱼管理系统通知', notification_type: str = 'info', attachment_path: Optional[str] = None) -> bool:
    result: Dict[str, bool] = {'sent': False}

    async def runner() -> None:
        result['sent'] = await dispatch_account_notifications(
            account_id,
            message,
            title=title,
            notification_type=notification_type,
            attachment_path=attachment_path,
        )

    def thread_main() -> None:
        try:
            result['sent'] = asyncio.run(runner())
        except Exception as exc:
            logger.error(f"【{account_id}】同步发送通知失败: {_safe_str(exc)}")
            result['sent'] = False

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(runner())
        return result['sent']

    thread = threading.Thread(target=thread_main, daemon=True)
    thread.start()
    thread.join()
    return result['sent']
