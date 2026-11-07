"""消息推送：对接 MQTT 推送中心 /api/push。"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from config import NotifyConfig

LOG = logging.getLogger("backup.notify")


class Notifier:
    def __init__(self, config: NotifyConfig):
        self.config = config

    def _push_to_mqtt(self, status: str) -> bool:
        if status == "ok":
            return self.config.on_success_mqtt
        return self.config.on_failure_mqtt

    def send(self, status: str, title: str, message: str) -> bool:
        """status 必须是 ok / warn / error / info；失败只告警，不影响备份。"""
        if not self.config.enabled:
            LOG.debug("推送未启用，跳过")
            return False
        if status not in {"ok", "warn", "error", "info"}:
            status = "info"

        payload = {
            "server_name": self.config.server_name,
            "category": self.config.category,
            "topic": self.config.topic,
            "title": title,
            "message": message,
            "status": status,
            "push_to_mqtt": self._push_to_mqtt(status),
            "msg_type": "markdown",
            "mode": self.config.mode,
        }

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.config.api_url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                if 200 <= response.status < 300:
                    LOG.info("推送成功：%s（HTTP %s）", title, response.status)
                    return True
                LOG.warning(
                    "推送返回非 2xx：HTTP %s %s", response.status, body[:500]
                )
                return False
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            LOG.warning("推送失败：HTTP %s %s", exc.code, body[:500])
            return False
        except Exception as exc:  # noqa: BLE001 - 通知失败不能影响备份
            LOG.warning("推送失败：%s", exc)
            return False
