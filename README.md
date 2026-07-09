# claude-tempo-log

Claude Code plugin za evidenciju sati u agenciji. Za svaki Claude Code session automatski bilježi **task ID, temu razgovora i stvarno odrađeno vrijeme** — spremno za unos u [Tempo](https://www.tempo.io/).

## Što radi

- **Forsira task ID**: na početku sessiona Claude te pita za task ID i podsjeća te uz svaki prompt dok ga ne daš. Ako prompt počinje task ključem (npr. `CED-2545: popravi login`), hvata se automatski bez pitanja.
- **Bilježi temu**: Claude tijekom rada jednom rečenicom zapiše na čemu se radi i ažurira je ako se fokus promijeni.
- **Mjeri stvarno vrijeme, ne wall-clock**: session otvoren od 10 do 22 h nije 12 sati rada. Aktivnost iz transkripta se reže u **time slotove**: pauza dulja od 30 minuta otvara novi slot, kraća pauza se broji u rad. Tri bloka rada tijekom dana = tri slota s realnim minutama.
- **Log**: `~/.claude/work-log.jsonl` — jedan redak po slotu (datum, task ID, tema, projekt, početak/kraj, minute). Podaci su uvijek lokalni i per-user; plugin nikamo ništa ne šalje.

## Instalacija

Preduvjeti: `jq` i `python3` (standardno na Linuxu/macOS-u).

### S GitHuba (za kolege)

U Claude Codeu:

```
/plugin marketplace add <github-user>/claude-tempo-log
/plugin install claude-tempo-log@tempo-tools
```

Novije verzije se povlače s `/plugin marketplace update tempo-tools`.

### Iz lokalnog direktorija (razvoj)

```
/plugin marketplace add /putanja/do/claude-tempo-log
/plugin install claude-tempo-log@tempo-tools
```

Nakon instalacije otvori novi session — Claude će odmah tražiti task ID.

## Izvještaj za Tempo

```
/claude-tempo-log:report            # danas
/claude-tempo-log:report --week     # zadnjih 7 dana
/claude-tempo-log:report --date 2026-07-08
/claude-tempo-log:report --slots    # + pojedinačni slotovi
```

ili direktno u shellu: `scripts/report.sh --week`

Stupac **Tempo** je dnevna suma po tasku zaokružena na 15 min (to unosiš u Tempo), **Točno** su izmjerene minute za kontrolu. Rad kraći od 15 min se zaokružuje na 15 da se ne izgubi.

Napomena: paralelni sessioni na više projekata se broje svaki za sebe (slotovi se mogu vremenski preklapati) — svaki task dobije svoje puno vrijeme.

## Pravila slotova

1. Slot počinje aktivnošću nakon tišine dulje od 30 min.
2. Pauza ≤ 30 min između bilo koje aktivnosti (tvoj prompt, Claudeov odgovor, tool call) ostaje unutar slota i broji se kao rad.
3. Trajanje slota = zadnja aktivnost − prva aktivnost slota.
4. Dugi autonomni rad Claudea ostaje jedan slot (transkript se kontinuirano puni).
5. Session bez ijednog tvog prompta se ne logira; `/clear` i resume ne dupliraju zapise (slotovi sessiona se uvijek iznova preračunaju i zamijene).

## Konfiguracija (env varijable)

| Varijabla | Default | Opis |
|---|---|---|
| `TEMPO_GAP_MIN` | `30` | Pauza (min) koja otvara novi slot |
| `TEMPO_TASK_REGEX` | `[A-Z][A-Z0-9]+-[0-9]+` | Oblik task ključa (JIRA stil) |
| `TEMPO_TASK_IGNORE` | `UTF\|ISO\|SHA\|MD\|RFC\|CVE\|HTTP\|TLS\|GPT` | Prefiksi koji se ne hvataju kao task ID |
| `TEMPO_LOG_FILE` | `~/.claude/work-log.jsonl` | Lokacija loga |
| `TEMPO_STATE_DIR` | `~/.claude/tempo-sessions` | Privremeni state po sessionu |

## Struktura

```
.claude-plugin/plugin.json       # manifest plugina
.claude-plugin/marketplace.json  # repo je ujedno i marketplace "tempo-tools"
hooks/hooks.json                 # SessionStart / UserPromptSubmit / SessionEnd
scripts/session-start.sh         # state + instrukcija Claudeu (task ID, tema)
scripts/prompt-check.sh          # forsiranje task ID-a + auto-capture
scripts/tempo-task               # helper kojim Claude zapisuje task/temu
scripts/session-end.sh|.py       # transkript → time slotovi → work-log.jsonl
scripts/report.sh|.py            # izvještaj za Tempo
commands/report.md               # /claude-tempo-log:report
```
