import abc
import asyncio
import inspect
import logging
import re
import sys
from difflib import SequenceMatcher
from itertools import groupby
from operator import attrgetter
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Iterable, List, MutableSequence, NamedTuple, NewType, Optional, TypeVar, Union

from quart.routing import BaseConverter

from .decorators import cached_property
from .exceptions import EpisodeNotFound, StreamNotFound
from .languages import Language
from .request import Request
from .stateful import BsonType, Expiring
from .utils import anext

log = logging.getLogger(__name__)

UID = NewType("UID", str)


class UIDConverter(BaseConverter):
    def to_python(self, value):
        return UID(value)

    def to_url(self, value):
        return super().to_url(value)


RE_UID_CLEANER = re.compile(r"[^a-z0-9一-龯]+")

T = TypeVar("T")


async def get_first(coros: Iterable[Awaitable[T]], predicate: Callable[[T], Union[bool, Awaitable[bool]]] = bool) -> Optional[T]:
    while coros:
        done, coros = await asyncio.wait(coros, return_when=asyncio.FIRST_COMPLETED)
        if done:
            result = next(iter(done)).result()
            res = predicate(result)
            if inspect.isawaitable(res):
                res = await res
            if res:
                for coro in coros:
                    coro.cancel()

                return result

    return None


def get_certainty(a: str, b: str) -> float:
    return round(SequenceMatcher(a=a, b=b).ratio(), 2)


class SearchResult(NamedTuple):
    anime: "Anime"
    certainty: float

    async def to_dict(self) -> Dict[str, Any]:
        return {"anime": await self.anime.to_dict(),
                "certainty": self.certainty}


VIDEO_MIME_TYPES = ("video/",)


class Source(NamedTuple):
    mime_type: str
    src: str


class Stream(Expiring, abc.ABC):
    INCLUDE_CLS = True
    ATTRS = ("external", "links", "poster")
    CHANGING_ATTRS = ("links",)
    EXPIRE_TIME = Expiring.HOUR

    PRIORITY = 100

    HOST = None

    def __repr__(self) -> str:
        return f"{type(self).__name__} Stream: {self._req}"

    @classmethod
    async def can_handle(cls, req: Request) -> bool:
        """Check whether this Stream class can handle the request.

        This operation shouldn't actually perform any expensive checks.
        It should merely check whether it's even possible for this Stream to extract
        anything from the request.

        The default implementation compares the Stream.HOST variable to the host
        of the request url (www. is stripped!).

        :param req: request to stream to check
        :return: true if this Stream may be able to extract something from the size, false otherwise
        """
        return (await req.yarl).host.lstrip("www.") == cls.HOST

    @property
    def persist(self) -> bool:
        """Whether this stream should be stored even if there are neither poster nor links in it

        :return: true to save anyway, false otherwise
        """
        return False

    @property
    @abc.abstractmethod
    async def external(self) -> bool:
        """Indicate whether the links provided by this Stream may be used externally.

        :return: true of the links may be used externally, false otherwise
        """
        ...

    @property
    @abc.abstractmethod
    async def links(self) -> List[str]:
        ...

    @cached_property
    async def poster(self) -> Optional[str]:
        return None

    @cached_property
    async def working(self) -> bool:
        try:
            return len(await self.links) > 0
        except asyncio.CancelledError:
            return False
        except Exception:
            log.exception(f"{self} Couldn't fetch links")
            return False

    @property
    async def working_external_self(self) -> Optional["Stream"]:
        if await self.external and await self.working:
            return self
        else:
            return None

    @staticmethod
    async def get_successful_links(sources: Union[Request, MutableSequence[Request]]) -> List[str]:
        if isinstance(sources, Request):
            sources = [sources]

        for source in sources:
            source.request_kwargs["allow_redirects"] = True

        async def source_check(req: Request) -> bool:
            if await req.head_success:
                content_type = (await req.head_response).content_type

                if not content_type:
                    log.debug(f"No content type for {source}")
                    return False

                if content_type.startswith(VIDEO_MIME_TYPES):
                    return True
            else:
                log.debug(f"{source} didn't make it (probably timeout)!")
                return False

        requests = await Request.all(sources, predicate=source_check)

        urls = []
        for req in requests:
            urls.append(await req.url)

        log.debug(f"found {len(urls)} working sources")
        return urls

    async def to_dict(self) -> Dict[str, BsonType]:
        links, poster = await asyncio.gather(self.links, self.poster)

        return {"type": type(self).__name__,
                "url": self._req._raw_url,
                "links": links,
                "poster": poster,
                "updated": self.last_update.isoformat()}


class Episode(Expiring, abc.ABC):
    ATTRS = ("stream", "host_url", "raw_streams", "streams", "poster", "host_url")
    CHANGING_ATTRS = ATTRS
    EXPIRE_TIME = 6 * Expiring.HOUR

    def __init__(self, req: Request):
        super().__init__(req)

    def __repr__(self) -> str:
        return f"{type(self).__name__} Ep.: {repr(self._req)}"

    @property
    def dirty(self) -> bool:
        if self._dirty:
            return True
        else:
            if hasattr(self, "_streams"):
                return any(stream.dirty for stream in self._streams)
            return False

    @dirty.setter
    def dirty(self, value: bool):
        self._dirty = value
        if hasattr(self, "_streams"):
            for stream in self._streams:
                stream.dirty = value

    @property
    @abc.abstractmethod
    async def raw_streams(self) -> List[str]:
        ...

    @cached_property
    async def streams(self) -> List[Stream]:
        from .streams import get_stream

        links = await self.raw_streams

        streams = list(filter(None, await asyncio.gather(*(anext(get_stream(Request(link))) for link in links))))

        streams.sort(key=attrgetter("PRIORITY"), reverse=True)
        return streams

    @cached_property
    async def sources(self) -> List[str]:
        sources = []
        streams = await self.streams
        stream_links = await asyncio.gather(*(stream.links for stream in streams))

        for links in stream_links:
            sources.extend(links)

        return sources

    @cached_property
    async def stream(self) -> Optional[Stream]:
        log.debug(f"{self} Searching for working stream...")

        all_streams = await self.streams
        all_streams.sort(key=attrgetter("PRIORITY"), reverse=True)

        for priority, streams in groupby(all_streams, attrgetter("PRIORITY")):
            streams = list(streams)
            log.info(f"Looking at {len(streams)} stream(s) with priority {priority}")

            working_stream = await get_first([stream.working_external_self for stream in streams])
            if working_stream:
                log.debug(f"Found working stream: {working_stream}")
                return working_stream

        log.debug(f"No working stream for {self}")

    async def get(self, index: int) -> Stream:
        streams = await self.streams
        if not 0 <= index < len(streams):
            raise StreamNotFound()

        return streams[index]

    @cached_property
    async def poster(self) -> Optional[str]:
        log.debug(f"{self} searching for poster")
        return await get_first([stream.poster for stream in await self.streams])

    def serialise_special(self, key: str, value: Any) -> BsonType:
        if key == "streams":
            # if there are no links/poster in a stream and it has already been "processed", get rid of it
            return [stream.state for stream in value if stream.persist or getattr(stream, "_links", True) or getattr(stream, "_poster", True)]
        elif key == "stream":
            return value.state

    @classmethod
    def get_stream(cls, data: BsonType) -> Optional[Stream]:
        m, c = data["cls"].rsplit(".", 1)
        module = sys.modules.get(m)
        if module:
            stream_cls = getattr(module, c)
            return stream_cls.from_state(data)

    @classmethod
    def deserialise_special(cls, key: str, value: BsonType) -> Any:
        if key == "streams":
            streams = []
            for stream in value:
                streams.append(cls.get_stream(stream))
            return streams
        elif key == "stream":
            return cls.get_stream(value)

    async def to_dict(self) -> Dict[str, BsonType]:
        raw_streams, stream, poster = await asyncio.gather(self.raw_streams, self.stream, self.poster)

        return {"embeds": raw_streams,
                "stream": await stream.to_dict() if stream else None,
                "poster": poster,
                "updated": self.last_update.isoformat()}


class Anime(Expiring, abc.ABC):
    EPISODE_CLS = Episode

    INCLUDE_CLS = True
    ATTRS = ("id", "is_dub", "language", "title", "episode_count", "episodes", "last_update")
    CHANGING_ATTRS = ("episode_count",)
    EXPIRE_TIME = 30 * Expiring.MINUTE  # 30 mins should be fine, right?

    _episodes: Dict[int, EPISODE_CLS]

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:
        if hasattr(self, "_uid"):
            return self._uid
        else:
            return repr(self._req)

    def __str__(self) -> str:
        if hasattr(self, "_title"):
            return self._title
        else:
            return repr(self)

    def __eq__(self, other: "Anime") -> bool:
        return hash(self) == hash(other)

    def __hash__(self) -> int:
        if hasattr(self, "_uid"):
            return hash(self._uid)
        return hash(self._req)

    @property
    def dirty(self) -> bool:
        if self._dirty:
            return True
        else:
            if hasattr(self, "_episodes"):
                return any(ep.dirty for ep in self._episodes.values())
            return False

    @dirty.setter
    def dirty(self, value: bool):
        self._dirty = value
        if hasattr(self, "_episodes"):
            for ep in self._episodes.values():
                ep.dirty = value

    @cached_property
    async def uid(self) -> UID:
        name = RE_UID_CLEANER.sub("", type(self).__name__.lower())
        anime = RE_UID_CLEANER.sub("", (await self.title).lower())

        lang = (await self.language).value
        dubbed = "_dub" if await self.is_dub else ""

        return UID(f"{name}-{anime}-{lang}{dubbed}")

    @property
    async def id(self) -> UID:
        return await self.uid

    @id.setter
    def id(self, value: UID):
        self._uid = value

    @property
    @abc.abstractmethod
    async def is_dub(self) -> False:
        ...

    @property
    @abc.abstractmethod
    async def language(self) -> Language:
        ...

    @property
    @abc.abstractmethod
    async def title(self) -> str:
        ...

    @cached_property
    async def episode_count(self) -> int:
        return len(await self.get_episodes())

    @property
    async def episodes(self) -> Dict[int, EPISODE_CLS]:
        if hasattr(self, "_episodes"):
            if len(self._episodes) != await self.episode_count:
                log.info(f"{self} doesn't have all episodes. updating!")

                for i in range(await self.episode_count):
                    if i not in self._episodes:
                        self._episodes[i] = await self.get_episode(i)
        else:
            eps = await self.get_episodes()
            self._episodes = dict(enumerate(eps))

        return self._episodes

    async def get(self, index: int) -> EPISODE_CLS:
        if hasattr(self, "_episodes"):
            ep = self._episodes.get(index)
            if ep is not None:
                return ep
        try:
            return (await self.episodes)[index]
        except KeyError:
            raise EpisodeNotFound(index, await self.episode_count)

    @abc.abstractmethod
    async def get_episodes(self) -> List[EPISODE_CLS]:
        ...

    @abc.abstractmethod
    async def get_episode(self, index: int) -> EPISODE_CLS:
        ...

    async def to_dict(self) -> Dict[str, BsonType]:
        uid, title, episode_count, is_dub, language = await asyncio.gather(self.uid, self.title, self.episode_count, self.is_dub, self.language)

        return {"uid": uid,
                "title": title,
                "episodes": episode_count,
                "dubbed": is_dub,
                "language": language.value,
                "updated": self.last_update.isoformat()}

    @classmethod
    @abc.abstractmethod
    async def search(cls, query: str, *, dubbed: bool = False, language: Language = Language.ENGLISH) -> AsyncIterator[SearchResult]:
        ...

    def serialise_special(self, key: str, value: Any) -> BsonType:
        if key == "episodes":
            return {str(i): ep.state for i, ep in value.items()}
        elif key == "language":
            return value.value

    @classmethod
    def deserialise_special(cls, key: str, value: BsonType) -> Any:
        if key == "episodes":
            return {int(i): cls.EPISODE_CLS.from_state(ep) for i, ep in value.items()}
        elif key == "language":
            return Language(value)
