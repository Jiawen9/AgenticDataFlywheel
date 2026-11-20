"""Exact preprocessing builds that produce the preceding release’s row results."""

# The keys and values each cover all six rule files hashed by processing_config.
# Checkpoint orchestration is the only change. Unknown builds retain their own
# digest, including any edit to the model reviewer, action mapper, or converter.
_CHECKPOINT_RELEASE = {
    "cbd1c3eb3e21bac4d57b86800192b0071e347eae5b2e5634956146f31ba1af91":
        "871041e1397b7a0bedd51b8955bf2b53e86741809427196a9ba12ac1fbbc0c9a",
    "a53e401ebb702c17afc2a53bb18d7fac646b290e74bc36c6acad9494faeecb63":
        "68ffa0bb5dbec7a9c94376e45a2ab49ced4b276933deab00b1a76c718a7fa4e3",
    "d17df59a7d0c540e4527f80a5d7dad6122d3036237d09a76f2f38859f8ed8f65":
        "543b9fdd4a821f3d6505369e6e9f6d07e7cff921ed09d7e6f4c4682cfaed6cc8",
    "e340ab67ff8aa06f72602ceb7a9f6fe4942228bd0020a1e96d882b944733069e":
        "871041e1397b7a0bedd51b8955bf2b53e86741809427196a9ba12ac1fbbc0c9a",
    "a103ac6c598d8ccec9a6a7d83f1a3c44fbab325b737e4460af1a3fb8c0a597a8":
        "871041e1397b7a0bedd51b8955bf2b53e86741809427196a9ba12ac1fbbc0c9a",
    "6169c005a1f4b516575547417d472fe8152c439bfa090a4bb1b899d915971193":
        "871041e1397b7a0bedd51b8955bf2b53e86741809427196a9ba12ac1fbbc0c9a",
    "46c46fc7aff8f1b47369eaa497a01602a6c612f11311bec8bea733a28e166932":
        "871041e1397b7a0bedd51b8955bf2b53e86741809427196a9ba12ac1fbbc0c9a",
    "327ae8de870c773267acdb12f72168f435b839c590f1bbbfcf12046c3bf5a4cf":
        "68ffa0bb5dbec7a9c94376e45a2ab49ced4b276933deab00b1a76c718a7fa4e3",
    "0f566e40ded2e2590f5d6e8efe4fb601b317447fa46e59f630cc53de37053d11":
        "68ffa0bb5dbec7a9c94376e45a2ab49ced4b276933deab00b1a76c718a7fa4e3",
    "e569ffb00aadf85812f1b2ab2473ac98248fe36f58ea0f78ce25f074bdd6599a":
        "68ffa0bb5dbec7a9c94376e45a2ab49ced4b276933deab00b1a76c718a7fa4e3",
    "fe59962fa68c64f9208f34e14a7c9893276fa8058db66a2c407dbb59e3ca59d5":
        "68ffa0bb5dbec7a9c94376e45a2ab49ced4b276933deab00b1a76c718a7fa4e3",
    "23410f3ce0d30a4046b137297c4e78d12b4c9aea0dd09c5fb28d973a906ac9ba":
        "543b9fdd4a821f3d6505369e6e9f6d07e7cff921ed09d7e6f4c4682cfaed6cc8",
    "c8b32d98671a2ac456aeaddb93dd5210a69cef4bb0ba573eb282b87e09354c6a":
        "871041e1397b7a0bedd51b8955bf2b53e86741809427196a9ba12ac1fbbc0c9a",
    "05987df739be924b65ef30c0d8d66f42b01c7f35742693fae42051092a556130":
        "871041e1397b7a0bedd51b8955bf2b53e86741809427196a9ba12ac1fbbc0c9a",
    "31b35c26fa6e0721966559cf6bfa45085515fae22dd065049657dc2542e95a01":
        "871041e1397b7a0bedd51b8955bf2b53e86741809427196a9ba12ac1fbbc0c9a",
    "7e21b94ef68d34a09bdd367011f32ae448e4a08264aefbb10b7912b879090f55":
        "871041e1397b7a0bedd51b8955bf2b53e86741809427196a9ba12ac1fbbc0c9a",
}


def compatible_revision(revision: str) -> str:
    return _CHECKPOINT_RELEASE.get(revision, revision)
