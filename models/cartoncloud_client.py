import json
import logging
import time
from typing import Any, Optional

import requests
from requests.exceptions import HTTPError, RequestException, Timeout


_logger = logging.getLogger(__name__)


class CartonCloudAPIError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, response_data: Optional[dict] = None):
        self.message = message
        self.status_code = status_code
        self.response_data = response_data
        super().__init__(self.message)


class CartonCloudClient:
    """
    Multi-tenant CartonCloud API client.
    
    Usage:
        # Always provide explicit tenant
        tenant = env['cartoncloud.tenant'].browse(tenant_id)
        client = CartonCloudClient(env, tenant)
        
        # Or get tenant by warehouse
        tenant = env['cartoncloud.tenant'].get_tenant_by_warehouse(warehouse_id)
        client = CartonCloudClient(env, tenant)
    """
    def __init__(self, env, tenant, timeout: int = None):
        self.env = env
        self.timeout = timeout or 120
        self.tenant = tenant
        
        if not self.tenant:
            raise CartonCloudAPIError("CartonCloud tenant must be explicitly provided")

    def _get_gateway_host(self) -> str:
        if not self.tenant.gateway_host:
            raise CartonCloudAPIError("Missing CartonCloud gateway host")
        return self.tenant.gateway_host.rstrip("/")

    def _get_tenant_uuid(self) -> str:
        if not self.tenant.tenant_uuid:
            raise CartonCloudAPIError("Missing CartonCloud tenant UUID")
        return self.tenant.tenant_uuid

    def _get_api_version(self) -> str:
        return (self.tenant.api_version or "1").strip()

    def _get_customer_uuid(self) -> Optional[str]:
        return self.tenant.default_customer_uuid

    def _get_warehouse_uuid(self) -> Optional[str]:
        return self.tenant.default_warehouse_uuid

    def _get_client_id(self) -> str:
        if not self.tenant.auth_client_id:
            raise CartonCloudAPIError("Missing CartonCloud auth clientId")
        return self.tenant.auth_client_id

    def _get_client_secret(self) -> str:
        if not self.tenant.auth_client_secret:
            raise CartonCloudAPIError("Missing CartonCloud auth clientSecret")
        return self.tenant.auth_client_secret

    def _token_url(self) -> str:
        return f"{self._get_gateway_host()}/uaa/oauth/token"

    def _get_cached_token(self) -> Optional[str]:
        """Get cached token from tenant record"""
        return self.tenant.get_cached_token()

    def _cache_token(self, access_token: str, expires_in: Optional[int] = None) -> None:
        """Cache token in tenant record"""
        self.tenant.cache_token(access_token, expires_in)

    def get_access_token(self, force_refresh: bool = False) -> str:
        if not force_refresh:
            cached = self._get_cached_token()
            if cached:
                return cached

        client_id = self._get_client_id()
        client_secret = self._get_client_secret()
        url = self._token_url()

        data = {
            "grant_type": "client_credentials",
        }

        headers = {
            "Accept": "application/json",
        }

        try:
            response = requests.post(
                url=url,
                data=data,
                headers=headers,
                auth=(client_id, client_secret),
                timeout=self.timeout,
            )
            response.raise_for_status()

            try:
                payload = response.json()
            except ValueError as e:
                raise CartonCloudAPIError(
                    f"Invalid JSON from token endpoint: {response.text}",
                    status_code=response.status_code,
                ) from e

            access_token = payload.get("access_token")
            if not access_token:
                raise CartonCloudAPIError(
                    f"Token endpoint missing access_token: {json.dumps(payload)}",
                    status_code=response.status_code,
                    response_data=payload,
                )

            self._cache_token(access_token=access_token, expires_in=payload.get("expires_in"))
            return access_token

        except Timeout as e:
            raise CartonCloudAPIError(f"Token request timeout: {str(e)}") from e
        except HTTPError as e:
            raise CartonCloudAPIError(
                f"Token request failed HTTP {e.response.status_code}: {e.response.text}",
                status_code=e.response.status_code,
            ) from e
        except RequestException as e:
            raise CartonCloudAPIError(f"Token request failed: {str(e)}") from e

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path

        gateway_host = self._get_gateway_host()
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{gateway_host}{path}"

    def request(self, method: str, path: str, *, headers: Optional[dict[str, str]] = None, **kwargs: Any) -> Any:
        token = self.get_access_token()

        request_headers = (headers or {}).copy()
        request_headers.update(
            {
                "Accept": "application/json",
                "Accept-Version": self._get_api_version(),
                "Authorization": f"Bearer {token}",
            }
        )

        url = self._build_url(path)

        try:
            _logger.info(f"CartonCloud API Request: {method} {url} (Tenant: {self._get_tenant_uuid()})")
            response = requests.request(
                method=method,
                url=url,
                headers=request_headers,
                timeout=kwargs.pop("timeout", self.timeout),
                **kwargs,
            )

            if response.status_code == 401:
                _logger.info("Token expired, refreshing...")
                token = self.get_access_token(force_refresh=True)
                request_headers["Authorization"] = f"Bearer {token}"
                response = requests.request(
                    method=method,
                    url=url,
                    headers=request_headers,
                    timeout=kwargs.pop("timeout", self.timeout),
                    **kwargs,
                )

            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                return response.json()

            return response.text

        except Timeout as e:
            raise CartonCloudAPIError(f"Request timeout: {method} {url} ({str(e)})") from e
        except HTTPError as e:
            raise CartonCloudAPIError(
                f"HTTP {e.response.status_code}: {e.response.text}",
                status_code=e.response.status_code,
            ) from e
        except RequestException as e:
            raise CartonCloudAPIError(f"Request failed: {str(e)}") from e

    def tenant_path(self, tenant_relative_path: str) -> str:
        tenant_uuid = self._get_tenant_uuid()
        if not tenant_relative_path.startswith("/"):
            tenant_relative_path = f"/{tenant_relative_path}"
        return f"/tenants/{tenant_uuid}{tenant_relative_path}"