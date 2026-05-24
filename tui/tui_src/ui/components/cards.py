from textual.widgets import Static


class ProviderCard(Static):
    def __init__(self, provider_name: str, status: str = "unknown"):
        super().__init__(f"{provider_name}: {status}")
