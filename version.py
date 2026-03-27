"""Shared version metadata for FlexSpotBridge."""

APP_NAME = "FlexSpotBridge"
APP_VERSION = "1.2.0"
APP_PRERELEASE = ""


def app_version_label() -> str:
    if APP_PRERELEASE:
        return f"{APP_VERSION}-{APP_PRERELEASE}"
    return APP_VERSION
