"""Live weather from Open-Meteo (status: live; free for non-commercial use, no key).

Commercial use requires an Open-Meteo subscription (https://open-meteo.com/en/pricing). Every answer
carries the source, the observation time in the place's own timezone, and units.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx

from apna.core.errors import ProviderUnavailable

_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "violent showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


def describe(code: int | None) -> str:
    return _WMO.get(int(code), "unknown conditions") if code is not None else "unknown conditions"


@dataclass
class Place:
    name: str
    region: str | None
    country: str | None
    latitude: float
    longitude: float
    timezone: str


@dataclass
class DayForecast:
    day: date
    t_min: float
    t_max: float
    rain_chance: int | None
    conditions: str


@dataclass
class Weather:
    place: Place
    observed_at_local: str  # ISO local time from the provider
    timezone: str
    temperature: float
    feels_like: float | None
    humidity: int | None
    wind_kmh: float | None
    precipitation_mm: float | None
    conditions: str
    units: dict[str, str]
    days: list[DayForecast]
    source: str = "Open-Meteo (open-meteo.com)"


class OpenMeteo:
    def __init__(self, base_url: str, geocoding_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base, self.geo, self._transport = base_url.rstrip("/"), geocoding_url.rstrip("/"), transport

    async def _get(self, url: str, params: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=15, transport=self._transport) as c:
                r = await c.get(url, params=params)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Weather service unreachable: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise ProviderUnavailable(f"Weather service error {r.status_code}")
        return r.json()

    async def geocode(self, name: str) -> Place | None:
        for extra in ({"countryCode": "IN"}, {}):  # prefer India, then anywhere
            j = await self._get(f"{self.geo}/v1/search", {"name": name, "count": 1, "language": "en",
                                                          "format": "json", **extra})
            if j.get("results"):
                r = j["results"][0]
                return Place(r["name"], r.get("admin1"), r.get("country"), r["latitude"], r["longitude"],
                             r.get("timezone") or "auto")
        return None

    async def forecast(self, place: Place, days: int = 4) -> Weather:
        j = await self._get(f"{self.base}/v1/forecast", {
            "latitude": place.latitude, "longitude": place.longitude, "timezone": place.timezone,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,"
                       "wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_days": days,
        })
        cur, units, d = j["current"], j.get("current_units", {}), j.get("daily", {})
        out = [DayForecast(date.fromisoformat(t), d["temperature_2m_min"][i], d["temperature_2m_max"][i],
                           (d.get("precipitation_probability_max") or [None] * len(d["time"]))[i],
                           describe(d["weather_code"][i])) for i, t in enumerate(d.get("time", []))]
        return Weather(place=place, observed_at_local=cur["time"], timezone=j.get("timezone", place.timezone),
                       temperature=cur["temperature_2m"], feels_like=cur.get("apparent_temperature"),
                       humidity=cur.get("relative_humidity_2m"), wind_kmh=cur.get("wind_speed_10m"),
                       precipitation_mm=cur.get("precipitation"), conditions=describe(cur.get("weather_code")),
                       units={"temperature": units.get("temperature_2m", "°C"),
                              "wind": units.get("wind_speed_10m", "km/h")}, days=out)
