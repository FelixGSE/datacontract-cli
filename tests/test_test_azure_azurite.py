"""``datacontract test`` reads parquet from Azure storage, with Azurite standing in for the account.

Only a connection string reaches the emulator (Azurite takes bearer tokens over HTTPS alone and
never checks their signature), so this covers the duckdb secret, a location that names only the
container (which a connection string allows) and the parquet read; the credential chain itself
is covered in test_connect_azure_credentials.py.
"""

import duckdb
from testcontainers.community.azurite import AzuriteContainer

from datacontract.data_contract import DataContract

CONTAINER = "orders"

DATA_CONTRACT = f"""
apiVersion: v3.1.0
kind: DataContract
id: orders-azurite
name: Orders
version: 1.0.0
status: active
servers:
  - server: azurite
    type: azure
    location: az://{CONTAINER}/*.parquet
    format: parquet
schema:
  - name: orders
    properties:
      - name: order_id
        logicalType: string
        required: true
        unique: true
      - name: amount
        logicalType: integer
        required: true
        logicalTypeOptions:
          minimum: 0
"""


def test_test_azure_parquet(monkeypatch, tmp_path):
    from azure.storage.blob import BlobServiceClient

    parquet_file = tmp_path / "orders.parquet"
    duckdb.sql(
        f"COPY (SELECT 'o-' || i AS order_id, i * 10 AS amount FROM range(1, 4) t(i)) "
        f"TO '{parquet_file}' (FORMAT PARQUET)"
    )
    with AzuriteContainer() as azurite_container:
        connection_string = azurite_container.get_connection_string()
        monkeypatch.setenv("DATACONTRACT_AZURE_CONNECTION_STRING", connection_string)
        blob_service_client = BlobServiceClient.from_connection_string(connection_string)
        blob_service_client.create_container(CONTAINER)
        with open(parquet_file, "rb") as file:
            blob_service_client.get_blob_client(CONTAINER, "orders.parquet").upload_blob(file)

        run = DataContract(data_contract_str=DATA_CONTRACT).test()

    assert run.result == "passed"
    assert all(check.result == "passed" for check in run.checks)
    assert any(check.field == "amount" for check in run.checks)
