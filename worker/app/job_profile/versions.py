SCHEMA_VERSION = "job-profile-v1.1"
PROJECTOR_VERSION = "native-v1.1"
GEOGRAPHY_VERSION = "geography-v1.1"
CLEANER_VERSION = "description-v1.0"
PARSER_VERSION = "deterministic-v1.0"
DICTIONARY_VERSION = "technology-v1.0"
TAXONOMY_VERSION = "unmapped-v1.0"


def versions():
    return {
        "schema": SCHEMA_VERSION, "projector": PROJECTOR_VERSION,
        "cleaner": CLEANER_VERSION, "parser": PARSER_VERSION,
        "dictionary": DICTIONARY_VERSION, "taxonomy": TAXONOMY_VERSION,
        "geography": GEOGRAPHY_VERSION,
    }
