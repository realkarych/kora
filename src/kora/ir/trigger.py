from typing import Annotated, Literal

from pydantic import MISSING, Field, StrictStr

from kora.ir.config import SensorPoll
from kora.ir.data import IrJsonObjectField
from kora.ir.record import IrRecord
from kora.ir.scalars import Duration, EnvName


class WebhookAuth(IrRecord):
    kind: Literal["hmac", "bearer"]
    secret: EnvName


class ManualTrigger(IrRecord):
    kind: Literal["manual"]
    params: IrJsonObjectField | MISSING = MISSING


class CronTrigger(IrRecord):
    kind: Literal["cron"]
    expr: StrictStr
    timezone: StrictStr | MISSING = MISSING
    params: IrJsonObjectField | MISSING = MISSING


class WebhookTrigger(IrRecord):
    kind: Literal["webhook"]
    name: StrictStr
    auth: WebhookAuth
    params_from: Literal["payload", "none"] | MISSING = MISSING
    params: IrJsonObjectField | MISSING = MISSING


class SensorTrigger(IrRecord):
    kind: Literal["sensor"]
    poll: SensorPoll
    interval: Duration
    params: IrJsonObjectField | MISSING = MISSING


class GraphTrigger(IrRecord):
    kind: Literal["graph"]
    graph: StrictStr
    on: Literal["run_succeeded", "run_failed", "run_finished"]
    params: IrJsonObjectField | MISSING = MISSING


Trigger = Annotated[
    ManualTrigger | CronTrigger | WebhookTrigger | SensorTrigger | GraphTrigger,
    Field(discriminator="kind"),
]
