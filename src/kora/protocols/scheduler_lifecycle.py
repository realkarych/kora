class SchedulerFailure(RuntimeError):
    def __init__(self) -> None:
        super().__init__("scheduler work could not settle")
