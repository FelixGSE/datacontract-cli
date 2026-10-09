"""Resolve Azure storage credentials for the connections that need them handed over.

The ``DATACONTRACT_AZURE_*`` options win when set: a connection string, a storage
account key, or a service principal. With none of them, ``DefaultAzureCredential``
resolves whatever identity the machine has — ``az login``, the ``AZURE_*`` variables,
workload identity federation on a pod, a managed identity on a VM or in CI.

duckdb cannot run that chain itself: its ``PROVIDER credential_chain`` is a
narrower C++ chain, and ``delta_scan`` does not honour it for workload identity
at all. So, as for S3 (``aws_credentials``), the credential is resolved here and
duckdb gets a static secret — an access token for the storage scope. The blob
metadata checks hand the same resolution to the Azure SDK client.
"""

import re
from dataclasses import dataclass

from datacontract.config import Config, env_name
from datacontract.model.exceptions import DataContractException

SIGN_IN_HINT = (
    "Sign in with `az login`, set the AZURE_* or DATACONTRACT_AZURE_* variables, "
    "and make sure the identity has the Storage Blob Data Reader role on the storage account."
)


@dataclass(frozen=True)
class AzureCredentials:
    """A connection string (configured, or built from a storage account key), or else a token credential."""

    connection_string: str | None = None
    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    # <account>.<service>.<suffix> from the location; a connection string names the account itself
    account_host: str | None = None

    @property
    def service_principal(self) -> bool:
        return bool(self.tenant_id and self.client_id and self.client_secret)

    def token_credential(self):
        """The ``azure.identity`` credential: the service principal if configured, else the default chain."""
        try:
            from azure.identity import ClientSecretCredential, DefaultAzureCredential
        except ImportError as exc:
            raise DataContractException(
                type="azure-connection",
                name="azure extra missing",
                reason="Install the extra datacontract-cli[azure] to connect to Azure storage",
                original_exception=exc,
            )
        try:
            if self.service_principal:
                return ClientSecretCredential(
                    tenant_id=self.tenant_id, client_id=self.client_id, client_secret=self.client_secret
                )
            return DefaultAzureCredential()
        except ValueError as exc:  # a malformed tenant id, an unknown AZURE_TOKEN_CREDENTIALS value
            raise DataContractException(
                type="azure-connection",
                name="Azure credential settings invalid",
                reason=str(exc),
                original_exception=exc,
            )

    def storage_access_token(self) -> str:
        """An access token for Azure storage, as duckdb's ``PROVIDER access_token`` secret expects it."""
        credential = self.token_credential()  # names the missing extra before anything else of the SDK is imported
        from azure.core.exceptions import ClientAuthenticationError

        try:
            return credential.get_token("https://storage.azure.com/.default").token
        except ClientAuthenticationError as exc:
            raise DataContractException(
                type="azure-connection",
                name="Azure authentication failed",
                reason=f"{exc.message}\n{SIGN_IN_HINT}",
                original_exception=exc,
            )


def resolve_azure_credentials(location: str | None, config: Config | None = None) -> AzureCredentials:
    """The configured ``DATACONTRACT_AZURE_*`` credentials for a location; none of them means the default chain."""
    config = Config.resolve(config)
    connection_string = config.get_azure_connection_string()
    if connection_string:
        return AzureCredentials(connection_string=connection_string)
    account_host = _storage_account_host(location)
    account_key = config.get_azure_storage_account_key()
    if account_key:
        # duckdb's azure secret has no account-key option; a connection string carries the key.
        account, _, endpoint_suffix = account_host.split(".", 2)
        return AzureCredentials(
            connection_string=f"DefaultEndpointsProtocol=https;AccountName={account};AccountKey={account_key};"
            f"EndpointSuffix={endpoint_suffix}"
        )
    credentials = AzureCredentials(
        tenant_id=config.get_azure_tenant_id(),
        client_id=config.get_azure_client_id(),
        client_secret=config.get_azure_client_secret(),
        account_host=account_host,
    )
    if credentials.service_principal or not (
        credentials.tenant_id or credentials.client_id or credentials.client_secret
    ):
        return credentials
    # A half-configured service principal would silently fall through to the default
    # chain and fail with an unrelated error; name the missing options instead.
    missing = [
        env_name(field, Config.model_fields[field])
        for field, value in (
            ("azure_tenant_id", credentials.tenant_id),
            ("azure_client_id", credentials.client_id),
            ("azure_client_secret", credentials.client_secret),
        )
        if not value
    ]
    raise DataContractException(
        type="azure-connection",
        name="Azure service principal incomplete",
        reason=f"Set {', '.join(missing)} as well to use a service principal, or unset the other service "
        "principal options to use the Azure credential chain (az login, workload identity, managed identity).",
    )


def _storage_account_host(location: str | None) -> str:
    """The ``<account>.<service>.<suffix>`` host a location names.

    ODCS has no field for the storage account, so it comes from the location:
    ``scheme://<container>@<account>.dfs.core.windows.net/<path>`` or
    ``scheme://<account>.blob.core.windows.net/<container>/<path>``. A bare
    ``scheme://<container>/<path>`` names no account; only a connection string,
    which carries the account itself, can serve it.
    """
    host = (location or "").partition("://")[2].partition("/")[0].rpartition("@")[2]
    # A hostname and nothing else: the account goes into duckdb's secret and, with an account key,
    # into a connection string, where a ';' from a contract someone else published would add keys.
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+){2,}", host, re.IGNORECASE):
        raise DataContractException(
            type="azure-connection",
            name="Azure storage account unknown",
            reason=f"The location '{location}' does not name the storage account. Use a fully qualified location "
            "such as abfss://<container>@<account>.dfs.core.windows.net/<path>, or set "
            "DATACONTRACT_AZURE_CONNECTION_STRING, which names the account itself.",
        )
    return host
