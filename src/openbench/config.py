from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_DATABASE_URL = "sqlite:///./.openbench/data/openbench.db"
DEFAULT_CAPTURE_DIRECTORY = ".openbench/data/captures/sessions"
DEFAULT_POLL_INTERVAL_S = 0.5
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_UT61E_POLL_INTERVAL_S = 0.5
DEFAULT_UT61EPLUS_POLL_INTERVAL_S = 1.0
DEFAULT_UT197_POLL_INTERVAL_S = 0.3
DEFAULT_UT197_SCAN_TIMEOUT_S = 10.0
DEFAULT_FEELTECH_POLL_INTERVAL_S = 1.0
DEFAULT_DPS150_POLL_INTERVAL_S = 0.5
DEFAULT_OWON_SPM_POLL_INTERVAL_S = 0.5
DEFAULT_ITECH_IT6000C_POLL_INTERVAL_S = 2.0
DEFAULT_MICSIG_DISCOVERY_TIMEOUT_S = 2.0
DEFAULT_MICSIG_SCPI_PORT = 5025
DEFAULT_MICSIG_HTTP_PORT = 80
DEFAULT_KINGST_DISCOVERY_TIMEOUT_S = 12.0


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _env_csv(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = DEFAULT_DATABASE_URL
    capture_directory: str = DEFAULT_CAPTURE_DIRECTORY
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    auto_discover: bool = False
    ut61e_enabled: bool = True
    ut61e_poll_interval_s: float = DEFAULT_UT61E_POLL_INTERVAL_S
    ut61eplus_enabled: bool = True
    ut61eplus_poll_interval_s: float = DEFAULT_UT61EPLUS_POLL_INTERVAL_S
    ut197_enabled: bool = True
    ut197_poll_interval_s: float = DEFAULT_UT197_POLL_INTERVAL_S
    ut197_scan_timeout_s: float = DEFAULT_UT197_SCAN_TIMEOUT_S
    feeltech_enabled: bool = True
    feeltech_poll_interval_s: float = DEFAULT_FEELTECH_POLL_INTERVAL_S
    dps150_enabled: bool = True
    dps150_poll_interval_s: float = DEFAULT_DPS150_POLL_INTERVAL_S
    owon_spm_enabled: bool = True
    owon_spm_poll_interval_s: float = DEFAULT_OWON_SPM_POLL_INTERVAL_S
    itech_it6000c_enabled: bool = True
    itech_it6000c_auto_discover: bool = False
    itech_it6000c_poll_interval_s: float = DEFAULT_ITECH_IT6000C_POLL_INTERVAL_S
    micsig_enabled: bool = True
    micsig_hosts: tuple[str, ...] = ()
    micsig_scan_subnets: tuple[str, ...] = ()
    micsig_scan_fallback: bool = True
    micsig_discovery_timeout_s: float = DEFAULT_MICSIG_DISCOVERY_TIMEOUT_S
    micsig_scpi_port: int = DEFAULT_MICSIG_SCPI_PORT
    micsig_http_port: int = DEFAULT_MICSIG_HTTP_PORT
    micsig_eto_enabled: bool = True
    micsig_eto_auto_discover: bool = False
    micsig_eto_hosts: tuple[str, ...] = ()
    micsig_eto_scan_subnets: tuple[str, ...] = ()
    micsig_eto_scan_fallback: bool = True
    micsig_eto_discovery_timeout_s: float = DEFAULT_MICSIG_DISCOVERY_TIMEOUT_S
    micsig_eto_scpi_port: int = DEFAULT_MICSIG_SCPI_PORT
    micsig_eto_http_port: int = DEFAULT_MICSIG_HTTP_PORT
    kingst_enabled: bool = True
    kingst_discovery_timeout_s: float = DEFAULT_KINGST_DISCOVERY_TIMEOUT_S
    sigrok_cli_path: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.getenv("OPENBENCH_DATABASE_URL", DEFAULT_DATABASE_URL),
            capture_directory=os.getenv(
                "OPENBENCH_CAPTURE_DIRECTORY",
                DEFAULT_CAPTURE_DIRECTORY,
            ),
            poll_interval_s=float(
                os.getenv("OPENBENCH_POLL_INTERVAL_S", str(DEFAULT_POLL_INTERVAL_S))
            ),
            host=os.getenv("OPENBENCH_HOST", DEFAULT_HOST),
            port=int(os.getenv("OPENBENCH_PORT", str(DEFAULT_PORT))),
            auto_discover=_env_bool("OPENBENCH_AUTO_DISCOVER", False),
            ut61e_enabled=_env_bool("OPENBENCH_UT61E_ENABLED", True),
            ut61e_poll_interval_s=float(
                os.getenv(
                    "OPENBENCH_UT61E_POLL_INTERVAL_S",
                    str(DEFAULT_UT61E_POLL_INTERVAL_S),
                )
            ),
            ut61eplus_enabled=_env_bool("OPENBENCH_UT61EPLUS_ENABLED", True),
            ut61eplus_poll_interval_s=float(
                os.getenv(
                    "OPENBENCH_UT61EPLUS_POLL_INTERVAL_S",
                    str(DEFAULT_UT61EPLUS_POLL_INTERVAL_S),
                )
            ),
            ut197_enabled=_env_bool("OPENBENCH_UT197_ENABLED", True),
            ut197_poll_interval_s=float(
                os.getenv(
                    "OPENBENCH_UT197_POLL_INTERVAL_S",
                    str(DEFAULT_UT197_POLL_INTERVAL_S),
                )
            ),
            ut197_scan_timeout_s=float(
                os.getenv(
                    "OPENBENCH_UT197_SCAN_TIMEOUT_S",
                    str(DEFAULT_UT197_SCAN_TIMEOUT_S),
                )
            ),
            feeltech_enabled=_env_bool("OPENBENCH_FEELTECH_ENABLED", True),
            feeltech_poll_interval_s=float(
                os.getenv(
                    "OPENBENCH_FEELTECH_POLL_INTERVAL_S",
                    str(DEFAULT_FEELTECH_POLL_INTERVAL_S),
                )
            ),
            dps150_enabled=_env_bool("OPENBENCH_DPS150_ENABLED", True),
            dps150_poll_interval_s=float(
                os.getenv(
                    "OPENBENCH_DPS150_POLL_INTERVAL_S",
                    str(DEFAULT_DPS150_POLL_INTERVAL_S),
                )
            ),
            owon_spm_enabled=_env_bool("OPENBENCH_OWON_SPM_ENABLED", True),
            owon_spm_poll_interval_s=float(
                os.getenv(
                    "OPENBENCH_OWON_SPM_POLL_INTERVAL_S",
                    str(DEFAULT_OWON_SPM_POLL_INTERVAL_S),
                )
            ),
            itech_it6000c_enabled=_env_bool("OPENBENCH_ITECH_IT6000C_ENABLED", True),
            itech_it6000c_auto_discover=_env_bool(
                "OPENBENCH_ITECH_IT6000C_AUTO_DISCOVER",
                False,
            ),
            itech_it6000c_poll_interval_s=float(
                os.getenv(
                    "OPENBENCH_ITECH_IT6000C_POLL_INTERVAL_S",
                    str(DEFAULT_ITECH_IT6000C_POLL_INTERVAL_S),
                )
            ),
            micsig_enabled=_env_bool("OPENBENCH_MICSIG_ENABLED", True),
            micsig_hosts=_env_csv("OPENBENCH_MICSIG_HOSTS"),
            micsig_scan_subnets=_env_csv("OPENBENCH_MICSIG_SCAN_SUBNETS"),
            micsig_scan_fallback=_env_bool(
                "OPENBENCH_MICSIG_SCAN_FALLBACK",
                True,
            ),
            micsig_discovery_timeout_s=float(
                os.getenv(
                    "OPENBENCH_MICSIG_DISCOVERY_TIMEOUT_S",
                    str(DEFAULT_MICSIG_DISCOVERY_TIMEOUT_S),
                )
            ),
            micsig_scpi_port=int(
                os.getenv(
                    "OPENBENCH_MICSIG_SCPI_PORT",
                    str(DEFAULT_MICSIG_SCPI_PORT),
                )
            ),
            micsig_http_port=int(
                os.getenv(
                    "OPENBENCH_MICSIG_HTTP_PORT",
                    str(DEFAULT_MICSIG_HTTP_PORT),
                )
            ),
            micsig_eto_enabled=_env_bool("OPENBENCH_MICSIG_ETO_ENABLED", True),
            micsig_eto_auto_discover=_env_bool(
                "OPENBENCH_MICSIG_ETO_AUTO_DISCOVER",
                False,
            ),
            micsig_eto_hosts=_env_csv("OPENBENCH_MICSIG_ETO_HOSTS"),
            micsig_eto_scan_subnets=_env_csv("OPENBENCH_MICSIG_ETO_SCAN_SUBNETS"),
            micsig_eto_scan_fallback=_env_bool(
                "OPENBENCH_MICSIG_ETO_SCAN_FALLBACK",
                True,
            ),
            micsig_eto_discovery_timeout_s=float(
                os.getenv(
                    "OPENBENCH_MICSIG_ETO_DISCOVERY_TIMEOUT_S",
                    str(DEFAULT_MICSIG_DISCOVERY_TIMEOUT_S),
                )
            ),
            micsig_eto_scpi_port=int(
                os.getenv(
                    "OPENBENCH_MICSIG_ETO_SCPI_PORT",
                    str(DEFAULT_MICSIG_SCPI_PORT),
                )
            ),
            micsig_eto_http_port=int(
                os.getenv(
                    "OPENBENCH_MICSIG_ETO_HTTP_PORT",
                    str(DEFAULT_MICSIG_HTTP_PORT),
                )
            ),
            kingst_enabled=_env_bool("OPENBENCH_KINGST_ENABLED", True),
            kingst_discovery_timeout_s=float(
                os.getenv(
                    "OPENBENCH_KINGST_DISCOVERY_TIMEOUT_S",
                    str(DEFAULT_KINGST_DISCOVERY_TIMEOUT_S),
                )
            ),
            sigrok_cli_path=os.getenv("OPENBENCH_SIGROK_CLI") or None,
        )
