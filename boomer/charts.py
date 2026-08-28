"""PNG charts for the Slack report. Optional: without matplotlib the bot falls back to sparklines."""
import datetime
import io
import logging

from boomer.stats import Stats

logger = logging.getLogger(__name__)

# Nobody is around at 4 a.m.: hourly charts cover the working day only
CHART_HOURS = range(9, 20)

# Sub-hour bars: the busy stretches of an afternoon do not show at hour granularity
HOUR_SLOTS = 4
_SLOT_LABEL = "par heure" if HOUR_SLOTS == 1 else f"par {60 // HOUR_SLOTS} min"
_TOP_SOUNDS = 5
_DAY_LABELS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
_MAX_LEGEND_LABEL = 22

try:
    import matplotlib
    matplotlib.use("Agg")  # the Pi has no display
    from matplotlib import pyplot as plt
except ImportError:
    plt = None


def available() -> bool:
    return plt is not None


def _x_labels(period: str, size: int) -> list[str]:
    if period == "day":
        return [f"{hour}h" for hour in CHART_HOURS]
    if period == "week":
        return _DAY_LABELS[:size]
    # A month is too crowded for one label per day
    return [str(day) if day == 1 or day % 5 == 0 else "" for day in range(1, size + 1)]


def elapsed_slots(period: str, size: int) -> int:
    """Slots already behind us. Drawing the rest of the period would trail a flat zero line."""
    now = datetime.datetime.now()
    if period == "day":
        return min(size, max(1, now.hour - CHART_HOURS.start + 1))
    if period == "week":
        return now.weekday() + 1
    return min(size, now.day)


def _legend_label(name: str, total: int) -> str:
    trimmed = name if len(name) <= _MAX_LEGEND_LABEL else name[:_MAX_LEGEND_LABEL - 1] + "…"
    return f"{trimmed} ({total})"


def render(stats: Stats, period: str, title: str) -> bytes | None:
    """The top sounds over the period, plus when the day is loudest. None if there is nothing to draw."""
    if plt is None or period == "all":
        return None
    totals = dict(stats.top_sounds(period, limit=_TOP_SOUNDS))
    if not totals:
        return None
    series = {}
    for name in totals:
        counts = stats.timeline(period, sound=name)
        if period == "day":
            counts = [counts[hour] for hour in CHART_HOURS]
        series[name] = counts
    size = elapsed_slots(period, len(next(iter(series.values()))))
    labels = _x_labels(period, len(next(iter(series.values()))))[:size]
    series = {name: counts[:size] for name, counts in series.items()}

    hourly = period != "day"
    try:
        fig, axes = plt.subplots(
            2 if hourly else 1, 1, figsize=(9, 6.4 if hourly else 3.6), dpi=110,
            gridspec_kw={"height_ratios": [2, 1.3], "hspace": 0.4} if hourly else None,
        )
        top_ax, hour_ax = (axes[0], axes[1]) if hourly else (axes, None)

        slots = range(len(next(iter(series.values()))))
        for name, counts in series.items():
            top_ax.plot(slots, counts, marker="o", markersize=4, linewidth=2,
                        label=_legend_label(name, totals[name]))
        top_ax.set_xticks(list(slots), labels)
        top_ax.set_ylim(bottom=0)
        top_ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
        top_ax.grid(axis="y", alpha=0.25)
        top_ax.spines[["top", "right"]].set_visible(False)
        top_ax.set_title(title, fontsize=12, loc="left")
        top_ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1.0), frameon=False, fontsize=9)

        if hour_ax is not None:
            slots_of_day = stats.by_hour(period, per_hour=HOUR_SLOTS)
            first = CHART_HOURS.start * HOUR_SLOTS
            counts = slots_of_day[first:CHART_HOURS.stop * HOUR_SLOTS]
            hour_ax.bar(range(len(counts)), counts, color="#7a5cd1", width=0.85)
            hour_ax.set_xticks([(hour - CHART_HOURS.start) * HOUR_SLOTS for hour in CHART_HOURS],
                               [f"{hour}h" for hour in CHART_HOURS])
            hour_ax.set_xticks(range(len(counts)), minor=True)
            hour_ax.tick_params(axis="x", which="minor", length=2)
            hour_ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
            hour_ax.grid(axis="y", alpha=0.25)
            hour_ax.spines[["top", "right"]].set_visible(False)
            hour_ax.set_title(f"Heures chaudes ({_SLOT_LABEL})", fontsize=11, loc="left")

        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", bbox_inches="tight", facecolor="white")
        return buffer.getvalue()
    except Exception:
        logger.exception("Cannot render the %s chart", period)
        return None
    finally:
        plt.close("all")
