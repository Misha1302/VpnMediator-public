from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

HAPP_OFFICIAL_PAGE_URL = "https://www.happ.su/main"
HAPP_GITHUB_ORG_URL = "https://github.com/Happ-proxy"

ALLOWED_HAPP_HOSTS = frozenset(
    {
        "www.happ.su",
        "happ.su",
        "happ.info",
        "www.happ.info",
        "apps.apple.com",
        "play.google.com",
        "github.com",
        "tv.happ.su",
    }
)

ALLOWED_GITHUB_PREFIX = "https://github.com/Happ-proxy"


class Platform(StrEnum):
    ANDROID = "android"
    IOS = "ios"
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    ANDROID_TV = "android_tv"
    APPLE_TV = "apple_tv"
    ROUTER = "router"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ClientAppLink:
    label: str
    url: str
    primary: bool = False


@dataclass(frozen=True)
class ClientAppPlatform:
    platform: Platform
    title_ru: str
    install_links: tuple[ClientAppLink, ...]
    fallback_url: str = HAPP_OFFICIAL_PAGE_URL
    verified_deep_link_template: str | None = None

    def primary_link(self) -> ClientAppLink:
        for link in self.install_links:
            if link.primary:
                return link

        return ClientAppLink("Открыть официальный сайт Happ", self.fallback_url, primary=True)


class ClientAppCatalog:
    def __init__(self, platforms: dict[Platform, ClientAppPlatform]) -> None:
        self._platforms = platforms
        self.validate()

    @classmethod
    def default(cls) -> ClientAppCatalog:
        landing = ClientAppLink("Открыть официальный сайт Happ", HAPP_OFFICIAL_PAGE_URL, True)
        github = ClientAppLink("Скачать с официального GitHub Happ", HAPP_GITHUB_ORG_URL)
        platforms = {
            Platform.ANDROID: ClientAppPlatform(
                Platform.ANDROID,
                "Android",
                (
                    landing,
                    github,
                ),
            ),
            Platform.IOS: ClientAppPlatform(
                Platform.IOS,
                "iPhone / iPad",
                (landing,),
            ),
            Platform.WINDOWS: ClientAppPlatform(
                Platform.WINDOWS,
                "Windows",
                (
                    github,
                    landing,
                ),
            ),
            Platform.MACOS: ClientAppPlatform(
                Platform.MACOS,
                "Mac",
                (
                    landing,
                    github,
                ),
            ),
            Platform.LINUX: ClientAppPlatform(
                Platform.LINUX,
                "Linux",
                (
                    github,
                    landing,
                ),
            ),
            Platform.ANDROID_TV: ClientAppPlatform(
                Platform.ANDROID_TV,
                "Android TV / Google TV",
                (
                    landing,
                    github,
                ),
            ),
            Platform.APPLE_TV: ClientAppPlatform(
                Platform.APPLE_TV,
                "Apple TV",
                (landing,),
            ),
            Platform.ROUTER: ClientAppPlatform(
                Platform.ROUTER,
                "Роутер или другое устройство",
                (landing,),
            ),
            Platform.UNSUPPORTED: ClientAppPlatform(
                Platform.UNSUPPORTED,
                "Другое устройство",
                (landing,),
            ),
        }
        return cls(platforms)

    def get(self, platform: Platform | str) -> ClientAppPlatform:
        normalized = Platform(platform)
        return self._platforms[normalized]

    def validate(self) -> None:
        if not self._platforms:
            raise ValueError("Client app catalog must not be empty.")

        for entry in self._platforms.values():
            _validate_official_url(entry.fallback_url)

            if entry.verified_deep_link_template is not None:
                _validate_official_url(entry.verified_deep_link_template)

            for link in entry.install_links:
                _validate_official_url(link.url)


def _validate_official_url(url: str) -> None:
    parsed = urlsplit(url)

    if parsed.scheme != "https":
        raise ValueError("Client app links must use HTTPS.")

    host = (parsed.hostname or "").lower()

    if host not in ALLOWED_HAPP_HOSTS:
        raise ValueError(f"Client app link host is not allowlisted: {host}")

    if host == "github.com" and not url.startswith(ALLOWED_GITHUB_PREFIX):
        raise ValueError("GitHub links must point to the official Happ-proxy organization.")
