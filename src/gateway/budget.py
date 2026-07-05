"""Per team budget tracking with warning and hard cap thresholds."""

from __future__ import annotations

from dataclasses import dataclass, field

WARN_AT = 0.80


@dataclass
class BudgetState:
    daily_cap_usd: float
    spent_usd: float = 0.0
    warned: bool = False


@dataclass
class BudgetDecision:
    allowed: bool
    reason: str = ""
    warning: str = ""
    utilization: float = 0.0


class BudgetTracker:
    def __init__(self):
        self._teams: dict[str, BudgetState] = {}
        self.events: list[dict] = []

    def configure(self, team: str, daily_cap_usd: float) -> None:
        self._teams[team] = BudgetState(daily_cap_usd)

    def check(self, team: str) -> BudgetDecision:
        state = self._teams[team]
        utilization = state.spent_usd / state.daily_cap_usd
        if state.spent_usd >= state.daily_cap_usd:
            return BudgetDecision(
                False,
                reason=(f"daily budget of ${state.daily_cap_usd:.2f} reached;"
                        f" spent ${state.spent_usd:.4f}. Raise the cap through"
                        " the admin API or wait for the daily reset."),
                utilization=round(utilization, 3))
        decision = BudgetDecision(True, utilization=round(utilization, 3))
        if utilization >= WARN_AT and not state.warned:
            state.warned = True
            decision.warning = (f"team {team} at"
                                f" {utilization * 100:.0f} percent of budget")
            self.events.append({"team": team, "type": "budget_warning",
                                "utilization": round(utilization, 3)})
        return decision

    def record(self, team: str, cost_usd: float) -> None:
        self._teams[team].spent_usd += cost_usd

    def report(self) -> dict:
        return {team: {"cap": s.daily_cap_usd,
                       "spent": round(s.spent_usd, 4),
                       "utilization": round(s.spent_usd / s.daily_cap_usd, 3)}
                for team, s in self._teams.items()}

    def reset_daily(self) -> None:
        for state in self._teams.values():
            state.spent_usd, state.warned = 0.0, False
