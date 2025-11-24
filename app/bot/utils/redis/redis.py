import json

from redis.asyncio import Redis
from redis.exceptions import WatchError

from .models import UserData


class RedisStorage:
    """Class for managing user data storage using Redis."""

    NAME = "users"

    def __init__(self, redis: Redis) -> None:
        """
        Initializes the RedisStorage instance.

        :param redis: The Redis instance to be used for data storage.
        """
        self.redis = redis

    async def _get(self, name: str, key: str | int) -> bytes | None:
        """
        Retrieves data from Redis.

        :param name: The name of the Redis hash.
        :param key: The key to be retrieved.
        :return: The retrieved data or None if not found.
        """
        async with self.redis.client() as client:
            return await client.hget(name, str(key))

    async def _set(self, name: str, key: str | int, value: any) -> None:
        """
        Sets data in Redis.

        :param name: The name of the Redis hash.
        :param key: The key to be set.
        :param value: The value to be set.
        """
        async with self.redis.client() as client:
            await client.hset(name, str(key), value)

    async def _update_index(self, message_thread_id: int | None, user_id: int) -> None:
        """
        Updates the user index in Redis.

        :param message_thread_id: The ID of the message thread.
        :param user_id: The ID of the user to be updated in the index.
        """
        if message_thread_id is None:
            return
        index_key = f"{self.NAME}_index_{message_thread_id}"
        await self._set(index_key, user_id, "1")

    async def get_by_message_thread_id(self, message_thread_id: int) -> UserData | None:
        """
        Retrieves user data based on message thread ID.

        :param message_thread_id: The ID of the message thread.
        :return: The user data or None if not found.
        """
        user_id = await self._get_user_id_by_message_thread_id(message_thread_id)
        return None if user_id is None else await self.get_user(user_id)

    async def _get_user_id_by_message_thread_id(self, message_thread_id: int) -> int | None:
        """
        Retrieves user ID based on message thread ID.

        :param message_thread_id: The ID of the message thread.
        :return: The user ID or None if not found.
        """
        index_key = f"{self.NAME}_index_{message_thread_id}"
        async with self.redis.client() as client:
            user_ids = await client.hkeys(index_key)
            if not user_ids:
                return None
            raw = user_ids[0]
            if isinstance(raw, bytes):
                raw = raw.decode()
            return int(raw)

    async def get_user(self, id_: int) -> UserData | None:
        """
        Retrieves user data based on user ID.

        :param id_: The ID of the user.
        :return: The user data or None if not found.
        """
        data = await self._get(self.NAME, id_)
        if data is not None:
            # json.loads accepts bytes, but decoding to str makes conversions explicit
            if isinstance(data, (bytes, bytearray)):
                data = data.decode()
            decoded_data = json.loads(data)
            return UserData(**decoded_data)
        return None

    async def update_user(self, id_: int, data: UserData) -> None:
        """
        Updates user data in Redis.

        This method tries to perform an optimistic-locking update using WATCH/MULTI/EXEC,
        merging the provided data with any existing record to avoid lost updates. If the
        Redis client does not support WATCH in the current context (for example, test stubs),
        falls back to a simple hset.

        :param id_: The ID of the user to be updated.
        :param data: The updated user data.
        """
        json_data = json.dumps(data.to_dict())
        # Try to use WATCH/MULTI/EXEC for optimistic locking if the client supports it.
        async with self.redis.client() as client:
            # If client doesn't expose watch/multi_exec, fallback to simple set for compatibility.
            if not hasattr(client, "watch") or not hasattr(client, "multi_exec"):
                await client.hset(self.NAME, str(id_), json_data)
                await self._update_index(data.message_thread_id, id_)
                return

            max_retries = 5
            for attempt in range(max_retries):
                try:
                    # WATCH key
                    await client.watch(self.NAME)
                    raw = await client.hget(self.NAME, str(id_))
                    if raw is None:
                        current = {}
                    else:
                        if isinstance(raw, (bytes, bytearray)):
                            raw = raw.decode()
                        try:
                            current = json.loads(raw)
                        except Exception:
                            current = {}

                    # Merge current and new: prefer fields from `data`
                    merged = {**current, **data.to_dict()}

                    tr = client.multi_exec()
                    tr.hset(self.NAME, str(id_), json.dumps(merged))
                    await tr.execute()
                    # successful commit
                    await self._update_index(data.message_thread_id, id_)
                    return
                except WatchError:
                    # Concurrent modification: retry
                    continue
                finally:
                    # best-effort unwatch
                    try:
                        await client.unwatch()
                    except Exception:
                        pass

            # Fallback: if too many conflicts, write the provided snapshot
            await client.hset(self.NAME, str(id_), json_data)
            await self._update_index(data.message_thread_id, id_)

    async def get_all_users_ids(self) -> list[int]:
        """
        Retrieves all user IDs stored in the Redis hash.

        :return: A list of all user IDs.
        """
        async with self.redis.client() as client:
            user_ids = await client.hkeys(self.NAME)
            result: list[int] = []
            for user_id in user_ids:
                if isinstance(user_id, bytes):
                    decoded = user_id.decode()
                else:
                    decoded = str(user_id)
                try:
                    result.append(int(decoded))
                except ValueError:
                    # skip keys that are not numeric
                    continue
            return result

    async def get_banned_users(self) -> list[UserData]:
        """
        Retrieves all banned users.

        :return: A list of banned UserData objects.
        """
        all_user_ids = await self.get_all_users_ids()
        banned_users = []

        for user_id in all_user_ids:
            user_data = await self.get_user(user_id)
            if user_data and user_data.is_banned:
                banned_users.append(user_data)

        return banned_users