import json
import urllib.request

URL = (
    "https://api.dexscreener.com/"
    "latest/dex/search/?q=SOL"
)


def get_tokens():
    request = urllib.request.Request(
        URL,
        headers={
            "User-Agent": "SolanaMonitor/1.0"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=15
    ) as response:
        data = json.loads(
            response.read().decode()
        )

    return [
        pair
        for pair in data.get("pairs", [])
        if pair.get("chainId") == "solana"
    ]


def main():
    print("Solana Token Monitor")
    print("--------------------")

    try:
        tokens = get_tokens()

        for token in tokens[:10]:
            base = token.get(
                "baseToken",
                {}
            )

            name = base.get(
                "name",
                "Unknown"
            )

            symbol = base.get(
                "symbol",
                "?"
            )

            price = token.get(
                "priceUsd",
                "N/A"
            )

            print(
                f"{name} ({symbol}) - ${price}"
            )

    except Exception as error:
        print(
            f"Error: {error}"
        )


if __name__ == "__main__":
    main()
