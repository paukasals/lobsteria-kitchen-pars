# LOBSTERIA kitchen pars

Every night at 3 am Miami time, GitHub Actions runs `pars.py`:

1. Reads the last 13 weeks of Square orders for LOBSTERIA and The Crepes and Waffles Bar (Uber Eats and DoorDash orders come in through Square).
2. Groups them by service night (orders before 6 am count for the night before). A night with no orders in either location is treated as closed and skipped.
3. Converts every item, side and sauce into prep with the recipe map in `config.json` (new-menu portions).
4. Forecasts each item for the next 7 nights from the same weekday: last week 25%, 2 weeks ago 20%, 3 and 4 weeks ago 15% each, weeks 5–8 6.25% each. Nights an item sold out (from the close count) are skipped for that item.
5. Adds the buffer for that kind of item, subtracts the latest staff count (Google Form) and writes `pars.json`.
6. Builds the list for the next Restaurant Depot trip (Monday and Friday), covering demand until the trip after.

The kitchen iPad page reads `pars.json` from GitHub Pages. It shows tonight's pars, what to do, the buying list and the count form.

`pars.json` holds quantities only: no prices, no sales dollars, no customer data.

## Setup (once)
1. Repository settings → Secrets and variables → Actions → New repository secret:
   - `SQUARE_SECRET`: the Square production access token (Square Developer dashboard → your app → Credentials). The job only reads orders.
   - `COUNTS_CSV_URL`: the published CSV link of the count form's response sheet.
2. Settings → Pages → Deploy from a branch → `main` / root.
3. Actions tab → "Nightly pars" → Run workflow, to test it.

## Things to measure and set in `config.json`
- Prawns: clean yield of a 4 lb box (assumed 44.8 oz).
- Corvina: average fillet weight (assumed 8 oz).
- Oysters per box (buying assumes 60, the low end of 60–75).
- Fried fish Mixto: 2.5 oz corvina + 3 oz prawns (owner).

## Test locally
`FIXTURE=orders.json TODAY=2026-10-18T03:00 python3 pars.py`
