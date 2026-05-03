from __future__ import annotations

HARDCODED_DECKS: dict[str, dict[str, object]] = {
    "ember_vanguard": {
        "valid": True,
        "battlefields": ["Scorch Ridge", "Ashfall Bastion", "Cinder Gate"],
        "chosen_champion": "Kael, Ember Warden",
        "legend": "The Eternal Spark",
        "cards": [
            "Blazing Advance", "Blazing Advance", "Blazing Advance",
            "Cinder Spear", "Cinder Spear", "Cinder Spear",
            "Ashen Guard", "Ashen Guard", "Ashen Guard",
            "Magma Burst", "Magma Burst", "Magma Burst",
            "Flare Ritual", "Flare Ritual", "Flare Ritual",
            "Forge Sentinel", "Forge Sentinel", "Forge Sentinel",
            "Inferno Tactician", "Inferno Tactician", "Inferno Tactician",
            "Scorching Volley", "Scorching Volley", "Scorching Volley",
            "Pyre Channeler", "Pyre Channeler", "Pyre Channeler",
            "Kindle Resolve", "Kindle Resolve", "Kindle Resolve",
            "Ember Scout", "Ember Scout", "Ember Scout",
            "Volcanic Oath", "Volcanic Oath", "Volcanic Oath",
            "Riftfire Crest", "Riftfire Crest", "Riftfire Crest",
        ],
        "runes": [{"domain": "Fury"} for _ in range(12)],
    },
    "tide_wardens": {
        "valid": True,
        "battlefields": ["Moonwake Shore", "Coral Keep", "Tideglass Harbor"],
        "chosen_champion": "Nyra, Wavecaller",
        "legend": "Song of the Deep",
        "cards": [
            "Tidal Insight", "Tidal Insight", "Tidal Insight",
            "Coral Bastion", "Coral Bastion", "Coral Bastion",
            "Riptide Lancer", "Riptide Lancer", "Riptide Lancer",
            "Depthcall Adept", "Depthcall Adept", "Depthcall Adept",
            "Moonwake Barrier", "Moonwake Barrier", "Moonwake Barrier",
            "Harbor Skirmisher", "Harbor Skirmisher", "Harbor Skirmisher",
            "Undertow Snare", "Undertow Snare", "Undertow Snare",
            "Pearl Navigator", "Pearl Navigator", "Pearl Navigator",
            "Foamblade Duelist", "Foamblade Duelist", "Foamblade Duelist",
            "Current Shepherd", "Current Shepherd", "Current Shepherd",
            "Siren's Warning", "Siren's Warning", "Siren's Warning",
            "Stormglass Oracle", "Stormglass Oracle", "Stormglass Oracle",
            "Abyssal Accord", "Abyssal Accord", "Abyssal Accord",
        ],
        "runes": [{"domain": "Fury"} for _ in range(6)] + [{"domain": "Body"} for _ in range(6)],
    },
}

