"""Run each real preset once locally; no database, broker, or synthetic delays."""

from time import perf_counter

from computegrid_api.compute_demo import presets
from computegrid_api.config import get_compute_demo_settings
from computegrid_api.jobs.execution import computation_for


def main() -> None:
    for preset in presets(get_compute_demo_settings()):
        started = perf_counter()
        computation_for(preset.type.value)(preset.parameters.model_dump(mode="json"))
        print(f"{preset.type.value}: {perf_counter() - started:.3f}s", flush=True)


if __name__ == "__main__":
    main()
