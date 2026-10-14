"""The UE station (live), from the repository root on the UE laptop, as your normal user:

    .venv/bin/python -m separate_laptops.ue_station.app                                     # over the air
    .venv/bin/python -m separate_laptops.ue_station.app --profile configs/station_ue_rfsim.yaml  # RF simulator
"""

from separate_laptops.station_ui import live_main

if __name__ == "__main__":
    live_main("ue")
