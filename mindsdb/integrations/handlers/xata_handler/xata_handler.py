from collections import OrderedDict
from typing import List, Optional

import pandas as pd
import json
import xata
from xata.helpers import BulkProcessor

from mindsdb.integrations.libs.const import HANDLER_CONNECTION_ARG_TYPE as ARG_TYPE
from mindsdb.integrations.libs.response import RESPONSE_TYPE
from mindsdb.integrations.libs.response import HandlerResponse
from mindsdb.integrations.libs.response import HandlerResponse as Response
from mindsdb.integrations.libs.response import HandlerStatusResponse as StatusResponse
from mindsdb.integrations.libs.vectordatabase_handler import (
    FilterCondition,
    FilterOperator,
    TableField,
    VectorStoreHandler,
)
from mindsdb.utilities import log


class XataHandler(VectorStoreHandler):
    """This handler handles connection and execution of the Xata statements."""

    name = "xata"

    def __init__(self, name: str, **kwargs):
        super().__init__(name)
        self._connection_data = kwargs.get("connection_data")
        self._client_config = {
            "db_url": self._connection_data.get("db_url"),
            "api_key": self._connection_data.get("api_key"),
        }
        self._create_table_params = {
            "dimension": self._connection_data.get("db_url", 8),
        }
        self._client = None
        self.is_connected = False
        self.connect()

    def __del__(self):
        if self.is_connected is True:
            self.disconnect()

    def connect(self):
        """Connect to a Xata database."""
        if self.is_connected is True:
            return self._client
        try:
            self._client = xata.XataClient(**self._client_config)
            self.is_connected = True
            return self._client
        except Exception as e:
            log.logger.error(f"Error connecting to Xata client: {e}!")
            self.is_connected = False

    def disconnect(self):
        """Close the database connection."""
        if self.is_connected is False:
            return
        self._client = None
        self.is_connected = False

    def check_connection(self):
        """Check the connection to the Xata database."""
        response_code = StatusResponse(False)
        need_to_close = self.is_connected is False
        # NOTE: no direct way to test this
        # try getting the user, if it fails, it means that we are not connected
        try:
            self._client.users().get()
            response_code.success = True
        except Exception as e:
            log.logger.error(f"Error connecting to Xata: {e}!")
            response_code.error_message = str(e)
        finally:
            if response_code.success is True and need_to_close:
                self.disconnect()
            if response_code.success is False and self.is_connected is True:
                self.is_connected = False
        return response_code

    def create_table(self, table_name: str, if_not_exists=True) -> HandlerResponse:
        """Create a table with the given name in the Xata database."""
        try:
            self._client.table().create(table_name)
            self._client.table().set_schema(
                table_name=table_name,
                payload={
                    "columns": [
                        {
                            "name": "embeddings",
                            "type": "vector",
                            "vector": {"dimension": self._create_table_params["dimension"]}
                        },
                        {"name": "content", "type": "text"},
                        {"name": "metadata", "type": "json"},
                    ]
                }
            )
        except Exception as e:
            return Response(
                resp_type=RESPONSE_TYPE.ERROR,
                error_message=f"Unable to create table '{table_name}': {e}",
            )
        return Response(resp_type=RESPONSE_TYPE.OK)

    def drop_table(self, table_name: str, if_exists=True) -> HandlerResponse:
        """Delete a table from the Xata database."""
        try:
            self._client.table().delete(table_name)
        except Exception as e:
            return Response(
                resp_type=RESPONSE_TYPE.ERROR,
                error_message=f"Error deleting table '{table_name}': {e}",
            )
        return Response(resp_type=RESPONSE_TYPE.OK)

    def get_columns(self, table_name: str) -> HandlerResponse:
        """Get columns of the given table"""
        # Vector stores have predefined columns
        try:
            # But at least try to see if the table is valid
            resp = self._client.table().get_columns(table_name)
            if not resp.is_success():
                raise Exception(f"Error getting columns: {resp['message']}")
        except Exception as e:
            return Response(
                resp_type=RESPONSE_TYPE.ERROR,
                error_message=f"{e}",
            )
        return super().get_columns(table_name)

    def get_tables(self) -> HandlerResponse:
        """Get the list of tables in the Xata database."""
        try:
            table_names = pd.DataFrame(
                columns=["TABLE_NAME"],
                data=[table_data["name"] for table_data in self._client.branch().get_details()["schema"]["tables"]],
            )
            return Response(
                resp_type=RESPONSE_TYPE.TABLE,
                data_frame=table_names
            )
        except Exception as e:
            return Response(
                resp_type=RESPONSE_TYPE.ERROR,
                error_message=f"Error getting list of tables: {e}",
            )

    def insert(self, table_name: str, data: pd.DataFrame, columns: List[str] = None) -> HandlerResponse:
        """ Insert data into the Xata database. """
        # Preprocess data
        data = data.to_dict("records")
        # Convert metadata to json
        if "metadata" in data.columns:
            data["metadata"] = data["metadata"].apply(json.loads)
        if len(data) > 1:
            # Bulk processing
            try:
                bp = BulkProcessor(self._client, throw_exception=True)
                bp.put_records(table_name, data)
                bp.flush_queue()
            except Exception as e:
                return Response(
                    resp_type=RESPONSE_TYPE.ERROR,
                    error_message=f"Error inserting data into '{table_name}': {e}",
                )
        elif len(data) == 0:
            # Skip
            return Response(resp_type=RESPONSE_TYPE.OK)
        elif "id" in data[0] and TableField.ID.value in columns:
            # If id present
            id = data[0]["id"]
            rest_of_data = data[0].copy()
            del rest_of_data["id"]
            try:
                resp = self._client.records().insert_with_id(
                    table_name=table_name,
                    record_id=id,
                    payload=rest_of_data,
                    create_only=True,
                    columns=columns
                )
                if not resp.is_success():
                    raise Exception(resp["message"])
            except Exception as e:
                return Response(
                    resp_type=RESPONSE_TYPE.ERROR,
                    error_message=f"Error inserting data into '{table_name}': {e}",
                )
        else:
            # If id not present
            try:
                resp = self._client.records().insert(
                    table_name=table_name,
                    payload=data[0],
                    columns=columns
                )
                if not resp.is_success():
                    raise Exception(resp["message"])
            except Exception as e:
                return Response(
                    resp_type=RESPONSE_TYPE.ERROR,
                    error_message=f"Error inserting data into '{table_name}': {e}",
                )
        return Response(resp_type=RESPONSE_TYPE.OK)








    def select(
        self,
        table_name: str,
        columns: List[str] = None,
        conditions: List[FilterCondition] = None,
        offset: int = None,
        limit: int = None,
    ) -> HandlerResponse:
        collection = self._client.get_collection(table_name)
        filters = self._translate_metadata_condition(conditions)
        # check if embedding vector filter is present
        vector_filter = (
            []
            if conditions is None
            else [
                condition
                for condition in conditions
                if condition.column == TableField.SEARCH_VECTOR.value
            ]
        )
        if len(vector_filter) > 0:
            vector_filter = vector_filter[0]
        else:
            vector_filter = None
        id_filters = None
        if conditions is not None:
            id_filters = [
                condition.value
                for condition in conditions
                if condition.column == TableField.ID.value
            ] or None

        if vector_filter is not None:
            # similarity search
            query_payload = {
                "where": filters,
                "query_embeddings": vector_filter.value
                if vector_filter is not None
                else None,
                "include": ["metadatas", "documents", "distances"],
            }
            if limit is not None:
                query_payload["n_results"] = limit

            result = collection.query(**query_payload)
            ids = result["ids"][0]
            documents = result["documents"][0]
            metadatas = result["metadatas"][0]
            distances = result["distances"][0]
        else:
            # general get query
            result = collection.get(
                ids=id_filters,
                where=filters,
                limit=limit,
                offset=offset,
            )
            ids = result["ids"]
            documents = result["documents"]
            metadatas = result["metadatas"]
            distances = None

        # project based on columns
        payload = {
            TableField.ID.value: ids,
            TableField.CONTENT.value: documents,
            TableField.METADATA.value: metadatas,
        }

        if columns is not None:
            payload = {
                column: payload[column]
                for column in columns
                if column != TableField.EMBEDDINGS.value
            }

        # always include distance
        if distances is not None:
            payload[TableField.DISTANCE.value] = distances
        result_df = pd.DataFrame(payload)
        return Response(resp_type=RESPONSE_TYPE.TABLE, data_frame=result_df)

    def update(
        self, table_name: str, data: pd.DataFrame, columns: List[str] = None
    ) -> HandlerResponse:
        """
        Update data in the Xata database.
        TODO: not implemented yet
        """
        return super().update(table_name, data, columns)

    def delete(
        self, table_name: str, conditions: List[FilterCondition] = None
    ) -> HandlerResponse:
        filters = self._translate_metadata_condition(conditions)
        # get id filters
        id_filters = [
            condition.value
            for condition in conditions
            if condition.column == TableField.ID.value
        ] or None

        if filters is None and id_filters is None:
            raise Exception("Delete query must have at least one condition!")
        collection = self._client.get_collection(table_name)
        collection.delete(ids=id_filters, where=filters)
        return Response(resp_type=RESPONSE_TYPE.OK)


connection_args = OrderedDict(
    db_url={
        "type": ARG_TYPE.STR,
        "description": "Xata database url with region, database and, optionally the branch information",
        "required": True,
    },
    api_key={
        "type": ARG_TYPE.STR,
        "description": "personal Xata API key",
        "required": True,
    },
    dimension={
        "type": ARG_TYPE.INT,
        "description": "default dimension of embeddings vector used to create table when using create (default=8)",
        "required": False,
    },
)

connection_args_example = OrderedDict(
    db_url="https://...",
    api_key="abc_def...",
    dimension=8
)
