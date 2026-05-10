# Leetify Data Crawler
## README
This project is used for UCSD COGS108 final project assignments. 
## Run Guide:
### Setup
1. Download Python and [UV(package managing)](https://docs.astral.sh/uv/)
2. Open up terminal, go to the root folder of the project and run 
   ```uv sync```
3. Put your leetify API key inside the ```.env``` file.
### Run
```python
uv run main.py <rating_min> <rating_max> <seed_steam_id> [max_players](optional)
```
The program will crawl data based on the input player (the seed), and then find other players by looking up the current player's previous matches.