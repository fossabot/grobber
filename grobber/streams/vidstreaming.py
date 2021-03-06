import logging
import re
from typing import List, Optional

from . import register_stream
from ..decorators import cached_property
from ..models import Stream
from ..request import Request
from ..utils import parse_js_json

log = logging.getLogger(__name__)

RE_EXTRACT_SETUP = re.compile(r"playerInstance\.setup\((.+?)\);", re.DOTALL)


def extract_player_data(text: str) -> dict:
    match = RE_EXTRACT_SETUP.search(text)
    if not match:
        return {}
    return parse_js_json(match.group(1))


class Vidstreaming(Stream):
    ATTRS = ("player_data",)

    HOST = "vidstreaming.io"

    @cached_property
    async def player_data(self) -> dict:
        data = extract_player_data(await self._req.text)
        if not data:
            log.debug(f"Couldn't find player data {self}")

        return data

    @cached_property
    async def poster(self) -> Optional[str]:
        link = (await self.player_data).get("image")
        if link and await Request(link).head_success:
            return link
        return None

    @cached_property
    async def links(self) -> List[str]:
        raw_sources = (await self.player_data).get("sources")
        if not raw_sources:
            return []

        sources = [Request(source["file"]) for source in raw_sources]
        log.debug(f"found sources {sources}")
        return await self.get_successful_links(sources)

    @cached_property
    async def external(self) -> bool:
        return True


register_stream(Vidstreaming)
