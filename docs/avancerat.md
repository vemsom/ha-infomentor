### Om BankID inte går att använda

Det finns ett avancerat alternativ, **Klistra in refresh-token**, för den som
redan har en sådan. En token kan skapas med
[`infomentor-mcp`](https://github.com/wicol/infomentor-mcp):

```sh
TMP_STATE="$(mktemp -d)"
PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring \
XDG_STATE_HOME="$TMP_STATE" \
  uv run infomentor-mcp auth login          # BankID i webbläsaren, en gång

jq -r .refresh_token "$TMP_STATE/infomentor-mcp/mobile-credentials.json"
rm -rf "$TMP_STATE"
```

Använd helst BankID-vägen i stället: den skapar en egen inloggning. InfoMentor
returnerar en ny refresh-token vid varje förnyelse och den gamla slutar gälla,
så två klienter som delar samma token slår ut varandra. Integrationen sparar
sin token i Home Assistants config entry och skriver över den när den roteras.

## Så fungerar det

Inloggningen följer samma väg som InfoMentors mobilapp: skolans SSO med BankID,
därefter en app-auktorisering som ger integrationen en egen OAuth-token. Bara
refresh-token sparas, i Home Assistants config entry, och den förnyas automatiskt
när den går ut. Access-token skickas aldrig vidare till webbinloggningen, och
inga tokens, QR-koder eller barnnamn skrivs till loggen.

Datan hämtas med en `DataUpdateCoordinator` var femtonde minut, en läsning per
barn: nyheter, notifieringar, kalender och (valfritt) fritidsregistreringar.
Det är enbart läsande anrop mot InfoMentors egna endpoints — samma som appen
använder.
