import logging
from urllib.parse import urlencode, urlparse, urlunparse
from uuid import uuid4

from sentry.http import safe_urlread
from sentry.mediators import Mediator, Param
from sentry.mediators.external_requests.util import send_and_save_sentry_app_request
from sentry.utils import json
from sentry.utils.cache import memoize

logger = logging.getLogger("sentry.mediators.external-requests")


class ConfigurationSettingsRequester(Mediator):
    """
    Makes a GET/POST request to another service to fetch/update the values for each field in the ConfigurationSettings schema
    """

    install = Param("sentry.models.SentryAppInstallation")
    uri = Param((str,))
    fields = Param(object, required=False, default={})
    http_method = Param(str, required=False, default="GET")

    def call(self):
        return self._make_request()

    def _build_url(self):
        urlparts = list(urlparse(self.sentry_app.webhook_url))
        urlparts[2] = self.uri
        if self.http_method == "GET":
            query = {"installationId": self.install.uuid}
            urlparts[4] = urlencode(query)
        return urlunparse(urlparts)

    def _make_request(self):
        try:
            data = self.body if self.http_method == "POST" else {}
            body = safe_urlread(
                send_and_save_sentry_app_request(
                    self._build_url(),
                    self.sentry_app,
                    self.install.organization_id,
                    "configuration_settings.requested",
                    headers=self._build_headers(),
                    method=self.http_method,
                    data=data,
                )
            )
            response = json.loads(body)
        except Exception as e:
            logger.info(
                "configuration-settings-requester.error",
                extra={
                    "sentry_app_slug": self.sentry_app.slug,
                    "install_uuid": self.install.uuid,
                    "uri": self.uri,
                    "error_message": str(e),
                },
            )
            response = {}

        return response

    def _build_headers(self):
        request_uuid = uuid4().hex

        return {
            "Content-Type": "application/json",
            "Request-ID": request_uuid,
            "Sentry-App-Signature": self.sentry_app.build_signature(""),
        }

    @memoize
    def body(self):
        body = {"fields": {}}
        for name, value in self.fields.items():
            body["fields"][name] = value

        body["installationId"] = self.install.uuid

        return json.dumps(body)

    @memoize
    def sentry_app(self):
        return self.install.sentry_app
