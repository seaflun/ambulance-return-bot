from __future__ import annotations

from dataclasses import dataclass

from .models import AmbulanceReturnRequest


@dataclass(frozen=True, slots=True)
class SiteAutomationResult:
    key: str
    name: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class SiteDefinition:
    key: str
    name: str
    url: str


SITE_DEFINITIONS = [
    SiteDefinition(
        "vehicle_mileage",
        "\u8eca\u8f1b\u91cc\u7a0b",
        "https://ppe.tyfd.gov.tw/Account/Login?ReturnUrl=%2FCarRecord%2FList",
    ),
    SiteDefinition("consumables", "\u4e00\u7ad9\u901a\u8017\u6750", "https://nfaemsap3.nfa.gov.tw/SSO/"),
    SiteDefinition("disinfection", "\u7dca\u6025\u6551\u8b77\u6d88\u6bd2", "https://emsdt.tyfd.gov.tw/EmmWeb/"),
    SiteDefinition(
        "duty_work_log",
        "\u6d88\u9632\u52e4\u52d9\u5de5\u4f5c\u7d00\u9304",
        "https://dutymgt.tyfd.gov.tw/tyfd119/login119",
    ),
]


class SiteAdapter:
    definition: SiteDefinition
    requires_manual_captcha = False

    @property
    def key(self) -> str:
        return self.definition.key

    @property
    def name(self) -> str:
        return self.definition.name

    def run(self, request: AmbulanceReturnRequest) -> SiteAutomationResult:
        raise NotImplementedError


class VehicleMileageAdapter(SiteAdapter):
    definition = SITE_DEFINITIONS[0]

    def run(self, request: AmbulanceReturnRequest) -> SiteAutomationResult:
        missing = "\u672a\u586b"
        detail = (
            "\u5df2\u5efa\u7acb\u672c\u6a5f\u96fb\u8166\u64cd\u4f5c\u4efb\u52d9\uff0c\u7b2c\u4e00\u7248\u4e0d\u81ea\u52d5\u9001\u51fa\u3002"
            f" \u5f85\u586b\uff1a\u8eca\u8f1b={request.vehicle or missing}\u3001"
            f"\u53f8\u6a5f={request.driver or missing}\u3001\u91cc\u7a0b={request.mileage or missing}\u3002"
        )
        return SiteAutomationResult(self.key, self.name, "local_pc_ready", detail)


class ConsumablesAdapter(SiteAdapter):
    definition = SITE_DEFINITIONS[1]
    requires_manual_captcha = True

    def run(self, request: AmbulanceReturnRequest) -> SiteAutomationResult:
        return SiteAutomationResult(
            self.key,
            self.name,
            "manual_captcha_required",
            f"\u6b64\u7ad9\u6709\u9a57\u8b49\u78bc\uff1b\u672c\u6a5f\u96fb\u8166\u700f\u89bd\u5668\u958b\u555f\u5f8c\u8acb\u4eba\u5de5\u767b\u5165\u4e26\u767b\u6253\u8017\u6750\uff1a{request.consumable_summary}",
        )


class DisinfectionAdapter(SiteAdapter):
    definition = SITE_DEFINITIONS[2]
    requires_manual_captcha = True

    def run(self, request: AmbulanceReturnRequest) -> SiteAutomationResult:
        return SiteAutomationResult(
            self.key,
            self.name,
            "manual_captcha_required",
            f"\u6b64\u7ad9\u6709\u9a57\u8b49\u78bc\uff1b\u672c\u6a5f\u96fb\u8166\u700f\u89bd\u5668\u958b\u555f\u5f8c\u8acb\u4eba\u5de5\u767b\u5165\u4e26\u767b\u6253\u6d88\u6bd2\u7d00\u9304\uff1a{request.disinfection}",
        )


class DutyWorkLogAdapter(SiteAdapter):
    definition = SITE_DEFINITIONS[3]

    def run(self, request: AmbulanceReturnRequest) -> SiteAutomationResult:
        missing = "\u672a\u586b"
        detail = (
            "\u5df2\u5efa\u7acb\u672c\u6a5f\u96fb\u8166\u64cd\u4f5c\u4efb\u52d9\uff0c\u7b2c\u4e00\u7248\u4e0d\u81ea\u52d5\u9001\u51fa\u3002"
            f" \u5f85\u586b\uff1a\u53f8\u6a5f={request.driver or missing}\u3001"
            f"\u51fa\u52e4\u8eca\u8f1b={request.vehicle or missing}\u3001\u5de5\u4f5c\u7d00\u9304={request.work_note}"
        )
        return SiteAutomationResult(self.key, self.name, "local_pc_ready", detail)


def default_adapters() -> list[SiteAdapter]:
    return [
        VehicleMileageAdapter(),
        ConsumablesAdapter(),
        DisinfectionAdapter(),
        DutyWorkLogAdapter(),
    ]
