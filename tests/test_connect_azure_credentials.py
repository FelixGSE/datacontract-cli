"""How the Azure duckdb connection resolves credentials.

No Azure involved: azure-identity is faked, and the secret the setup creates is asserted
on a real in-memory duckdb, so a statement duckdb rejects fails here. The point is that
`az login`, workload identity and a managed identity reach Azure storage the way a
service principal already does.
"""

from unittest.mock import MagicMock, patch

import duckdb
import pytest
from azure.core.exceptions import ClientAuthenticationError
from open_data_contract_standard.model import Server

from datacontract.engines.datacontract.check_azure_blob_file import _build_blob_service_client
from datacontract.engines.ibis.connections.azure_credentials import AzureCredentials
from datacontract.engines.ibis.connections.duckdb_connection import setup_azure_connection
from datacontract.model.exceptions import DataContractException

AZURE_ENV = [
    "DATACONTRACT_AZURE_CONNECTION_STRING",
    "DATACONTRACT_AZURE_STORAGE_ACCOUNT_KEY",
    "DATACONTRACT_AZURE_TENANT_ID",
    "DATACONTRACT_AZURE_CLIENT_ID",
    "DATACONTRACT_AZURE_CLIENT_SECRET",
]
LOCATION = "abfss://orders@myaccount.dfs.core.windows.net/parquet/*.parquet"
CONNECTION_STRING = (
    "DefaultEndpointsProtocol=https;AccountName=myaccount;AccountKey=a2V5;EndpointSuffix=core.windows.net"
)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for name in AZURE_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _service_principal(env):
    env.setenv("DATACONTRACT_AZURE_TENANT_ID", "tenant")
    env.setenv("DATACONTRACT_AZURE_CLIENT_ID", "client")
    env.setenv("DATACONTRACT_AZURE_CLIENT_SECRET", "spn-secret")


def _chain():
    """A DefaultAzureCredential that resolves, as after `az login` or on a pod with workload identity."""
    chain = MagicMock()
    chain.return_value.get_token.return_value.token = "chain-token"
    return chain


def _setup(chain=None, location=LOCATION):
    """Run the setup on a real in-memory duckdb and return the CREATE SECRET statement it ran."""
    con = MagicMock(wraps=duckdb.connect(database=":memory:"))  # records the statements; duckdb still runs them
    server = Server(server="production", type="azure", location=location, format="parquet")
    with patch("azure.identity.DefaultAzureCredential", chain or _chain()):
        setup_azure_connection(con, server, None)
    return con.sql.call_args.args[0]


def test_the_credential_chain_is_used_when_nothing_is_configured():
    chain = _chain()

    sql = _setup(chain)

    assert "PROVIDER 'ACCESS_TOKEN'" in sql
    assert "ACCESS_TOKEN 'chain-token'" in sql
    assert "ACCOUNT_NAME 'myaccount'" in sql
    chain.return_value.get_token.assert_called_once_with("https://storage.azure.com/.default")


def test_a_service_principal_takes_precedence_over_the_chain(env):
    _service_principal(env)
    chain = _chain()

    sql = _setup(chain)

    assert "PROVIDER 'SERVICE_PRINCIPAL'" in sql
    assert "TENANT_ID 'tenant'" in sql
    assert "CLIENT_SECRET 'spn-secret'" in sql
    assert "ACCOUNT_NAME 'myaccount'" in sql
    chain.assert_not_called()


def test_a_connection_string_takes_precedence_over_everything(env):
    _service_principal(env)
    env.setenv("DATACONTRACT_AZURE_CONNECTION_STRING", CONNECTION_STRING)

    sql = _setup()

    assert f"CONNECTION_STRING '{CONNECTION_STRING}'" in sql
    assert "PROVIDER" not in sql


def test_a_connection_string_is_not_blocked_by_a_stray_service_principal_variable(env):
    env.setenv("DATACONTRACT_AZURE_CONNECTION_STRING", CONNECTION_STRING)
    env.setenv("DATACONTRACT_AZURE_TENANT_ID", "tenant")

    sql = _setup()

    assert f"CONNECTION_STRING '{CONNECTION_STRING}'" in sql


def test_a_connection_string_serves_a_location_that_names_only_the_container(env):
    env.setenv("DATACONTRACT_AZURE_CONNECTION_STRING", CONNECTION_STRING)

    sql = _setup(location="az://orders/*.parquet")

    assert f"CONNECTION_STRING '{CONNECTION_STRING}'" in sql


@pytest.mark.parametrize(
    "location, endpoint_suffix",
    [
        (LOCATION, "core.windows.net"),
        ("abfss://orders@myaccount.dfs.core.chinacloudapi.cn/parquet/*.parquet", "core.chinacloudapi.cn"),
    ],
)
def test_an_account_key_becomes_a_connection_string_for_the_account_and_cloud_in_the_location(
    env, location, endpoint_suffix
):
    env.setenv("DATACONTRACT_AZURE_STORAGE_ACCOUNT_KEY", "a2V5")

    sql = _setup(location=location)

    assert f"AccountName=myaccount;AccountKey=a2V5;EndpointSuffix={endpoint_suffix}'" in sql


def test_a_location_without_the_account_needs_a_connection_string():
    with pytest.raises(DataContractException) as exc_info:
        _setup(location="az://orders/*.parquet")

    assert "DATACONTRACT_AZURE_CONNECTION_STRING" in exc_info.value.reason


def test_a_host_that_is_not_a_hostname_cannot_add_keys_to_the_connection_string(env):
    """The location comes from the contract, the key from the environment; keep them apart."""
    env.setenv("DATACONTRACT_AZURE_STORAGE_ACCOUNT_KEY", "a2V5")

    with pytest.raises(DataContractException, match="does not name the storage account"):
        _setup(location="abfss://orders@evil;BlobEndpoint=http://intranet:8080;x.dfs.core.windows.net/*.parquet")


def test_a_half_configured_service_principal_names_the_missing_variables(env):
    env.setenv("DATACONTRACT_AZURE_TENANT_ID", "tenant")

    with pytest.raises(DataContractException) as exc_info:
        _setup()

    assert "DATACONTRACT_AZURE_CLIENT_ID" in exc_info.value.reason
    assert "DATACONTRACT_AZURE_CLIENT_SECRET" in exc_info.value.reason


def test_a_failed_chain_says_how_to_sign_in():
    chain = _chain()
    chain.return_value.get_token.side_effect = ClientAuthenticationError("No credential in the chain worked")

    with pytest.raises(DataContractException) as exc_info:
        _setup(chain)

    assert "No credential in the chain worked" in exc_info.value.reason
    assert "az login" in exc_info.value.reason


def test_an_invalid_credential_setting_is_reported_as_such(env):
    env.setenv("AZURE_TOKEN_CREDENTIALS", "not-a-credential")

    with pytest.raises(DataContractException) as exc_info:
        AzureCredentials().token_credential()

    assert "AZURE_TOKEN_CREDENTIALS" in exc_info.value.reason


@pytest.mark.parametrize(
    "location",
    [
        LOCATION,
        "https://myaccount.blob.core.windows.net/orders/parquet/",
        "wasbs://orders@myaccount.blob.core.windows.net/parquet/",
    ],
)
def test_the_blob_client_uses_the_same_chain(location):
    chain = _chain()
    with patch("azure.identity.DefaultAzureCredential", chain), patch("azure.storage.blob.BlobServiceClient") as client:
        _build_blob_service_client(location, None)

    client.assert_called_once_with(account_url="https://myaccount.blob.core.windows.net", credential=chain.return_value)


@pytest.mark.parametrize(
    "variable, value",
    [("DATACONTRACT_AZURE_CONNECTION_STRING", CONNECTION_STRING), ("DATACONTRACT_AZURE_STORAGE_ACCOUNT_KEY", "a2V5")],
)
def test_the_blob_client_uses_the_connection_string_configured_or_built_from_the_account_key(env, variable, value):
    env.setenv(variable, value)
    with patch("azure.storage.blob.BlobServiceClient") as client:
        _build_blob_service_client(LOCATION, None)

    client.from_connection_string.assert_called_once_with(CONNECTION_STRING)
