from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.urls import reverse

from .feature_flags import FEATURE_FLAG_CHOICES, FEATURE_FLAG_DEFINITIONS


class FeatureFlag(models.Model):
    key = models.CharField(max_length=80, unique=True, choices=FEATURE_FLAG_CHOICES)
    enabled = models.BooleanField(default=False)
    description = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return self.key

    @property
    def default_enabled(self):
        return bool(FEATURE_FLAG_DEFINITIONS.get(self.key, {}).get("default", False))


class HouseholdQuerySet(models.QuerySet):
    def delete(self):
        from .signals import suspend_change_log_for_households

        household_ids = list(self.values_list("pk", flat=True))
        with suspend_change_log_for_households(household_ids):
            return super().delete()


class Household(models.Model):
    class DataMode(models.TextChoices):
        DEMO = "demo", "Demo"
        REAL = "real", "Real"

    class DisplayGranularity(models.TextChoices):
        AUTO = "auto", "Auto"
        MONTHLY = "monthly", "Monthly"
        YEARLY = "yearly", "Yearly"

    name = models.CharField(max_length=120, default="My household")
    data_mode = models.CharField(max_length=20, choices=DataMode.choices, default=DataMode.REAL)
    is_active = models.BooleanField(
        default=False,
        help_text="The household currently shown by the planner. Only one household can be active.",
    )
    hidden_attention_items = models.JSONField(default=dict, blank=True)
    starting_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    start_month = models.DateField(help_text="Use the first day of the month.")
    planning_months = models.PositiveSmallIntegerField(default=24)
    planning_years = models.PositiveSmallIntegerField(
        default=0,
        help_text="Set this for long-term planning. Use 0 to keep the month-based horizon.",
    )
    display_granularity = models.CharField(
        max_length=20,
        choices=DisplayGranularity.choices,
        default=DisplayGranularity.AUTO,
    )
    annual_inflation_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("2.00"),
        help_text="Annual inflation assumption used to show long-term projections in today's money.",
    )
    default_income_growth_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Default annual growth for income rules and income investments when they do not override it.",
    )
    default_operating_account = models.ForeignKey(
        "AssetAccount",
        on_delete=models.SET_NULL,
        related_name="default_for_households",
        blank=True,
        null=True,
        help_text="Default cash or savings account used for income and expense rules when a rule does not choose its own account.",
    )
    pension_tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("18.00"),
        help_text="Simple planning assumption for tax and deductions on pension income.",
    )
    income_tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Optional planning rate for income tax and social security, applied only to income rules marked taxable. 0 leaves income rules as entered (net).",
    )
    capital_gains_tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("25.00"),
        help_text="Simple planning assumption for tax drag on ETF/capital withdrawals.",
    )
    capital_income_allowance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("2000.00"),
        help_text="Annual Sparerpauschbetrag applied to capital income before capital tax. Use 2,000 for a jointly planned household, or 1,000 for a single person.",
    )
    vorabpauschale_basiszins_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("3.20"),
        help_text="Planning Basiszins for German Vorabpauschale in percent. Update when the official annual rate changes.",
    )
    church_tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Optional church tax planning rate. Keep at 0 when not applicable.",
    )
    solidarity_surcharge_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Optional solidarity surcharge planning rate. Keep at 0 when not applicable.",
    )
    health_insurance_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("11.00"),
        help_text="Simple planning assumption for health and care insurance on retirement income.",
    )
    fund_cash_goal_from_depot = models.BooleanField(
        default=False,
        help_text="When on, the yearly cash goal is treated as household spending and any shortfall after income is drawn from the depot, net of the capital-gains rate. Use this to model living off the portfolio. Avoid also modelling the same spending as separate expenses.",
    )
    emergency_fund_months = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Target months of recurring expenses to keep liquid. Set 0 to disable emergency-fund checks.",
    )
    currency = models.CharField(max_length=3, default="EUR")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = HouseholdQuerySet.as_manager()

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_active:
            Household.objects.exclude(pk=self.pk).filter(is_active=True).update(is_active=False)

    def delete(self, *args, **kwargs):
        from .signals import suspend_change_log_for_households

        with suspend_change_log_for_households([self.pk]):
            return super().delete(*args, **kwargs)

    @property
    def projection_months(self):
        if self.planning_years:
            return self.planning_years * 12
        return self.planning_months

    @property
    def resolved_display_granularity(self):
        if self.display_granularity != self.DisplayGranularity.AUTO:
            return self.display_granularity
        if self.projection_months > 60:
            return self.DisplayGranularity.YEARLY
        return self.DisplayGranularity.MONTHLY

    @property
    def baseline_snapshot(self):
        return self.snapshots.filter(is_baseline=True).first()


class Person(models.Model):
    class Role(models.TextChoices):
        ADULT = "adult", "Adult"
        CHILD = "child", "Child"
        DEPENDENT = "dependent", "Dependent"

    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="people")
    name = models.CharField(max_length=120)
    role = models.CharField(max_length=20, choices=Role.choices)
    birth_date = models.DateField(blank=True, null=True)
    active_from = models.DateField(blank=True, null=True)
    active_until = models.DateField(blank=True, null=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["role", "name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("planner:household_settings")


class Scenario(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="scenarios")
    name = models.CharField(max_length=140)
    liquid_balance_delta = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="One-time cash adjustment at the start of the scenario.",
    )
    monthly_income_delta = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Scenario-wide monthly income adjustment.",
    )
    monthly_expense_delta = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Scenario-wide monthly expense adjustment.",
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class CashGoal(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="cash_goals")
    name = models.CharField(max_length=140)
    annual_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Planned yearly household cash need in household currency.",
    )
    indexed_to_inflation = models.BooleanField(
        default=False,
        help_text="Increase this cash need from the start year using the household inflation assumption.",
    )
    start_year = models.PositiveSmallIntegerField(help_text="First projection year this goal applies to.")
    end_year = models.PositiveSmallIntegerField(blank=True, null=True, help_text="Optional last year this goal applies to.")
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start_year", "name"]

    def __str__(self):
        return self.name


class ImportBatch(models.Model):
    class Source(models.TextChoices):
        CSV_ACCOUNTS = "csv_accounts", "CSV accounts"
        CSV_DEPOT_HOLDINGS = "csv_depot_holdings", "CSV depot holdings"
        MONEYMONEY = "moneymoney", "MoneyMoney"
        YNAB = "ynab", "YNAB"

    class Status(models.TextChoices):
        DRY_RUN = "dry_run", "Dry run"
        FAILED = "failed", "Failed"
        APPLIED = "applied", "Applied"

    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="import_batches")
    source = models.CharField(max_length=30, choices=Source.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRY_RUN)
    filename = models.CharField(max_length=255, blank=True)
    row_count = models.PositiveIntegerField(default=0)
    valid_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    summary = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_source_display()} {self.created_at:%Y-%m-%d %H:%M}"


class BackupEvent(models.Model):
    class Action(models.TextChoices):
        BACKUP = "backup", "Backup"
        RESTORE = "restore", "Restore"

    class Status(models.TextChoices):
        STARTED = "started", "Started"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    action = models.CharField(max_length=20, choices=Action.choices)
    status = models.CharField(max_length=20, choices=Status.choices)
    filename = models.CharField(max_length=255, blank=True)
    pre_restore_filename = models.CharField(max_length=255, blank=True)
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_action_display()} {self.get_status_display()} {self.created_at:%Y-%m-%d %H:%M}"


class ChangeLogEntry(models.Model):
    class Action(models.TextChoices):
        CREATED = "created", "Created"
        UPDATED = "updated", "Updated"
        DELETED = "deleted", "Deleted"

    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="change_log_entries", blank=True, null=True)
    action = models.CharField(max_length=20, choices=Action.choices)
    model_name = models.CharField(max_length=120)
    object_pk = models.CharField(max_length=64, blank=True)
    object_label = models.CharField(max_length=220, blank=True)
    changed_fields = models.JSONField(default=list, blank=True)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["household", "-created_at"]),
            models.Index(fields=["model_name", "object_pk"]),
        ]

    def __str__(self):
        return f"{self.get_action_display()} {self.model_name} {self.object_label or self.object_pk}"


class AssumptionReview(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="assumption_reviews")
    key = models.CharField(max_length=220)
    label = models.CharField(max_length=220)
    reviewed_at = models.DateTimeField(auto_now=True)
    reviewed_by = models.CharField(max_length=120, blank=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["key"]
        constraints = [
            models.UniqueConstraint(fields=["household", "key"], name="unique_assumption_review_per_household_key"),
        ]
        indexes = [
            models.Index(fields=["household", "key"]),
            models.Index(fields=["household", "reviewed_at"]),
        ]

    def __str__(self):
        return f"{self.label} reviewed for {self.household}"


class Snapshot(models.Model):
    class SnapshotType(models.TextChoices):
        BASELINE = "baseline", "Baseline"
        ANNUAL = "annual", "Annual"
        PRE_IMPORT = "pre_import", "Pre-import"
        MANUAL = "manual", "Manual"

    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="snapshots")
    name = models.CharField(max_length=140)
    snapshot_type = models.CharField(max_length=20, choices=SnapshotType.choices, default=SnapshotType.MANUAL)
    is_baseline = models.BooleanField(default=False)
    snapshot_date = models.DateField()
    summary = models.JSONField(default=dict)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-snapshot_date", "-created_at"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.is_baseline:
            super().save(*args, **kwargs)
            return
        with transaction.atomic():
            super().save(*args, **kwargs)
            Snapshot.objects.filter(household=self.household, is_baseline=True).exclude(pk=self.pk).update(is_baseline=False)


class SnapshotReview(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="snapshot_reviews")
    baseline_snapshot = models.ForeignKey(Snapshot, on_delete=models.CASCADE, related_name="baseline_reviews")
    comparison_snapshot = models.ForeignKey(Snapshot, on_delete=models.CASCADE, related_name="comparison_reviews")
    title = models.CharField(max_length=140)
    review_date = models.DateField()
    planned_summary = models.TextField(blank=True)
    actual_summary = models.TextField(blank=True)
    lessons_learned = models.TextField(blank=True)
    next_actions = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-review_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["household", "baseline_snapshot", "comparison_snapshot"],
                name="unique_snapshot_review_pair_per_household",
            )
        ]

    def __str__(self):
        return self.title


class SnapshotReviewAction(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        DONE = "done", "Done"
        SKIPPED = "skipped", "Skipped"

    review = models.ForeignKey(SnapshotReview, on_delete=models.CASCADE, related_name="actions")
    title = models.CharField(max_length=180)
    owner = models.ForeignKey(Person, on_delete=models.SET_NULL, related_name="review_actions", blank=True, null=True)
    due_date = models.DateField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "due_date", "title"]

    def __str__(self):
        return self.title


class AssetAccount(models.Model):
    class AccountType(models.TextChoices):
        CASH = "cash", "Cash"
        SAVINGS = "savings", "Savings"
        DEPOT = "depot", "Depot"
        LOAN = "loan", "Loan"
        OTHER = "other", "Other"

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        MONEYMONEY = "moneymoney", "MoneyMoney"
        YNAB = "ynab", "YNAB"

    class DepotValuation(models.TextChoices):
        ACCOUNT_BALANCE = "account_balance", "Account balance"
        HOLDINGS_SUM = "holdings_sum", "Sum of holdings"

    class InterestCadence(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"
        YEARLY = "yearly", "Yearly"

    class OwnerType(models.TextChoices):
        HOUSEHOLD = "household", "Household"
        PERSON = "person", "Person"
        EXTERNAL = "external", "External / informational"

    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="accounts")
    name = models.CharField(max_length=140)
    account_type = models.CharField(max_length=20, choices=AccountType.choices)
    owner_type = models.CharField(
        max_length=20,
        choices=OwnerType.choices,
        default=OwnerType.HOUSEHOLD,
        help_text="Who legally owns this account. Child-owned accounts can be tracked without counting toward household retirement net worth.",
    )
    owner_person = models.ForeignKey(
        Person,
        on_delete=models.SET_NULL,
        related_name="owned_accounts",
        blank=True,
        null=True,
        help_text="Person owner for child or individual accounts.",
    )
    counts_in_household_net_worth = models.BooleanField(
        default=True,
        help_text="Include this account in household planning net worth, liquidity, FIRE, and retirement projections. Turn off for child-owned depots.",
    )
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=3, default="EUR")
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL)
    depot_valuation = models.CharField(
        max_length=30,
        choices=DepotValuation.choices,
        default=DepotValuation.ACCOUNT_BALANCE,
        help_text="For depot accounts, choose whether projections use the account balance or summed holdings.",
    )
    depot_annual_return_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Expected nominal annual depot return in percent. Used for projection growth; keep 0 for flat depot values.",
    )
    depot_annual_distribution_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="For depot accounts, expected annual cash distribution/dividend yield in percent, paid out (not reinvested) and taxed at the household capital-gains rate. This is separate from and on top of the price-growth assumption above; keep 0 for accumulating funds.",
    )
    depot_teilfreistellung_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("30.00"),
        help_text="German partial tax exemption for depot distributions and gains. Use 30% for equity funds, 0% for bonds, or an average for mixed depots.",
    )
    depot_vorabpauschale_enabled = models.BooleanField(
        default=False,
        help_text="Apply German Vorabpauschale planning tax for accumulating funds. Usually off for distributing funds or bonds.",
    )
    depot_distribution_cadence = models.CharField(
        max_length=20,
        choices=InterestCadence.choices,
        default=InterestCadence.QUARTERLY,
        help_text="For depot accounts, how often distributions are paid.",
    )
    savings_annual_interest_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="For savings accounts, annual gross interest rate in percent.",
    )
    savings_interest_cadence = models.CharField(
        max_length=20,
        choices=InterestCadence.choices,
        default=InterestCadence.MONTHLY,
        help_text="For savings accounts, how often interest is paid.",
    )
    savings_interest_tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("25.00"),
        help_text="Simple tax rate applied to savings interest. Defaults to 25%.",
    )
    institution = models.CharField(max_length=120, blank=True)
    as_of_date = models.DateField(blank=True, null=True)
    moneymoney_account_key = models.CharField(max_length=220, blank=True)
    ynab_account_id = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["account_type", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["household", "moneymoney_account_key"],
                condition=~Q(moneymoney_account_key=""),
                name="unique_moneymoney_account_key_per_household",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def signed_balance(self):
        if self.account_type == self.AccountType.LOAN:
            return -abs(self.balance)
        return self.balance

    @property
    def owner_label(self):
        if self.owner_type == self.OwnerType.PERSON and self.owner_person:
            return self.owner_person.name
        return self.get_owner_type_display()

    @property
    def is_liquid(self):
        return self.account_type in {self.AccountType.CASH, self.AccountType.SAVINGS}

    @property
    def is_invested(self):
        return self.account_type == self.AccountType.DEPOT

    @property
    def is_other_asset(self):
        return self.account_type == self.AccountType.OTHER

    @property
    def holdings_value(self):
        return sum((holding.current_value for holding in self.holdings.all()), Decimal("0.00"))

    @property
    def effective_balance(self):
        if self.account_type == self.AccountType.DEPOT and self.depot_valuation == self.DepotValuation.HOLDINGS_SUM:
            return self.holdings_value
        if self.account_type == self.AccountType.LOAN:
            return abs(self.balance)
        return self.balance

    @property
    def uses_holdings_valuation(self):
        return self.account_type == self.AccountType.DEPOT and self.depot_valuation == self.DepotValuation.HOLDINGS_SUM

    @property
    def depot_difference(self):
        if self.account_type != self.AccountType.DEPOT:
            return Decimal("0.00")
        return self.holdings_value - self.balance

    def sync_balance_from_holdings(self):
        """For depots valued by their holdings, keep the stored balance equal to
        the holdings sum so it can never drift from the value the projection
        uses. Mirrors Debt.save() keeping the loan account in sync. No-op (and
        does not zero a not-yet-populated depot) when there are no holdings."""
        if not (self.pk and self.uses_holdings_valuation and self.holdings.exists()):
            return
        holdings_value = self.holdings_value
        if self.balance != holdings_value:
            self.balance = holdings_value
            self.save(update_fields=["balance", "updated_at"])


class MoneyMoneyAccountMapping(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="moneymoney_account_mappings")
    source_key = models.CharField(max_length=220, blank=True)
    source_kind = models.CharField(max_length=20, blank=True)
    account_name = models.CharField(max_length=140)
    account_type = models.CharField(max_length=20, choices=AssetAccount.AccountType.choices, blank=True)
    import_enabled = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["source_kind", "account_name", "source_key"]
        constraints = [
            models.UniqueConstraint(
                fields=["household", "source_key"],
                condition=~Q(source_key=""),
                name="unique_moneymoney_source_key_per_household",
            ),
        ]

    def __str__(self):
        status = "enabled" if self.import_enabled else "disabled"
        return f"{self.account_name} -> {self.account_type or 'default'} ({status})"

    def save(self, *args, **kwargs):
        if not self.source_key and self.account_name:
            self.source_key = f"legacy-name:{self.account_name}"
        if not self.source_kind and self.source_key.startswith("legacy-name:"):
            self.source_kind = "legacy"
        super().save(*args, **kwargs)


class DepotHolding(models.Model):
    asset_account = models.ForeignKey(AssetAccount, on_delete=models.CASCADE, related_name="holdings")
    name = models.CharField(max_length=180)
    isin = models.CharField(max_length=20, blank=True)
    ticker = models.CharField(max_length=30, blank=True)
    asset_class = models.CharField(max_length=80, default="ETF")
    quantity = models.DecimalField(max_digits=14, decimal_places=6)
    latest_price = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="EUR")
    as_of_date = models.DateField(blank=True, null=True)
    payout_date = models.DateField(
        blank=True,
        null=True,
        help_text="Optional payout or maturity date, useful for individual bonds or target-maturity bond ETFs.",
    )
    payout_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        blank=True,
        null=True,
        help_text="Optional expected cash payout at maturity. Leave blank to use the current holding value.",
    )
    annual_distribution_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text=(
            "Expected annual cash distribution/dividend yield in percent for this specific holding, paid out "
            "(not reinvested) and taxed at the household capital-gains rate. Only used when the depot is valued "
            "by summed holdings; keep 0 for accumulating funds. Not a growth assumption -- see the depot's own "
            "return rate for that."
        ),
    )
    distribution_cadence = models.CharField(
        max_length=20,
        choices=AssetAccount.InterestCadence.choices,
        default=AssetAccount.InterestCadence.QUARTERLY,
        help_text="How often this holding pays its distribution.",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def current_value(self):
        return self.quantity * self.latest_price

    @property
    def expected_payout_amount(self):
        return self.payout_amount if self.payout_amount is not None else self.current_value

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.asset_account.sync_balance_from_holdings()

    def delete(self, *args, **kwargs):
        account = self.asset_account
        result = super().delete(*args, **kwargs)
        account.sync_balance_from_holdings()
        return result


class Debt(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="debts")
    account = models.OneToOneField(
        AssetAccount,
        on_delete=models.CASCADE,
        related_name="debt",
        limit_choices_to={"account_type": AssetAccount.AccountType.LOAN},
    )
    source_account = models.ForeignKey(
        AssetAccount,
        on_delete=models.SET_NULL,
        related_name="paid_debts",
        blank=True,
        null=True,
        help_text="Optional cash or savings account used for monthly payments and extra repayments. Blank uses the household default operating account.",
    )
    real_estate = models.ForeignKey(
        "RealEstate",
        on_delete=models.SET_NULL,
        related_name="debts",
        blank=True,
        null=True,
        help_text="Optional property this loan finances. A property can have several loans; all are paid off when it is sold.",
    )
    name = models.CharField(max_length=140)
    current_principal = models.DecimalField(max_digits=12, decimal_places=2)
    annual_interest_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="Nominal annual interest rate in percent, for example 3.60.",
    )
    monthly_payment = models.DecimalField(max_digits=12, decimal_places=2)
    start_month = models.DateField(blank=True, null=True)
    end_month = models.DateField(blank=True, null=True)
    fixed_interest_until = models.DateField(blank=True, null=True)
    refinance_from_month = models.DateField(
        blank=True,
        null=True,
        help_text="Optional override. If blank, refinancing starts the month after fixed interest ends.",
    )
    refinance_annual_interest_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        blank=True,
        null=True,
        help_text="Assumed annual interest rate after refinancing.",
    )
    refinance_monthly_payment = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        blank=True,
        null=True,
        help_text="Assumed monthly payment after refinancing.",
    )
    interest_only_until = models.DateField(
        blank=True,
        null=True,
        help_text="Optional. Until this month only interest is paid and the principal does not reduce.",
    )
    annual_extra_payment = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Optional yearly Sondertilgung paid on top of the monthly payment.",
    )
    extra_payment_month = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        help_text="Calendar month (1-12) the yearly extra payment is made. Defaults to the start month.",
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        errors = {}
        if self.current_principal is not None and self.current_principal < 0:
            errors["current_principal"] = "Principal cannot be negative."
        has_refi_rate = self.refinance_annual_interest_rate is not None
        has_refi_payment = self.refinance_monthly_payment is not None
        if has_refi_rate != has_refi_payment:
            errors["refinance_monthly_payment"] = (
                "Set both a refinance interest rate and a refinance payment, or leave both blank."
            )
        if self.refinance_from_month and self.fixed_interest_until and self.refinance_from_month < self.fixed_interest_until:
            errors["refinance_from_month"] = "Refinance cannot start before the fixed-interest period ends."
        if self.annual_extra_payment is not None and self.annual_extra_payment < 0:
            errors["annual_extra_payment"] = "Yearly extra payment cannot be negative."
        if self.extra_payment_month is not None and not (1 <= self.extra_payment_month <= 12):
            errors["extra_payment_month"] = "Extra payment month must be between 1 and 12."
        if (
            self.current_principal
            and self.current_principal > 0
            and self.monthly_payment is not None
            and self.annual_interest_rate is not None
        ):
            first_interest = self.current_principal * self.annual_interest_rate / Decimal("100") / Decimal("12")
            if self.monthly_payment <= first_interest:
                errors["monthly_payment"] = (
                    "Monthly payment must exceed the first month's interest of "
                    f"{first_interest.quantize(Decimal('0.01'))} so the balance amortizes."
                )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Keep the linked loan account in sync so the projection (which reads
        # current_principal) and the rest of the app (which reads the account
        # balance) never drift. This runs on every write path, not just the
        # debt form.
        if self.account_id and self.account.balance != self.current_principal:
            self.account.balance = self.current_principal
            self.account.save(update_fields=["balance"])

    @property
    def monthly_interest_rate(self):
        return self.annual_interest_rate / Decimal("100") / Decimal("12")

    @property
    def effective_refinance_from_month(self):
        if self.refinance_from_month:
            return self.refinance_from_month
        if self.fixed_interest_until:
            year = self.fixed_interest_until.year + (self.fixed_interest_until.month // 12)
            month = (self.fixed_interest_until.month % 12) + 1
            return self.fixed_interest_until.replace(year=year, month=month, day=1)
        return None


class IncomeInvestment(models.Model):
    class InvestmentType(models.TextChoices):
        SOLAR = "solar", "Solar"
        PRIVATE_LOAN = "private_loan", "Private loan"
        REAL_ESTATE = "real_estate", "Real estate"
        OTHER = "other", "Other"

    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="income_investments")
    name = models.CharField(max_length=140)
    investment_type = models.CharField(max_length=30, choices=InvestmentType.choices, default=InvestmentType.OTHER)
    principal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    source_account = models.ForeignKey(
        AssetAccount,
        on_delete=models.SET_NULL,
        related_name="funded_income_investments",
        blank=True,
        null=True,
        help_text=(
            "Cash or savings account the principal is paid from when the investment starts. "
            "Only applied to purchases at or after the projection start; leave blank for "
            "investments you already own and paid for."
        ),
    )
    monthly_income = models.DecimalField(max_digits=12, decimal_places=2)
    annual_growth_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        blank=True,
        null=True,
        default=None,
        help_text="Optional annual growth applied to monthly income. Leave blank to use the household default; set 0 to keep flat.",
    )
    currency = models.CharField(max_length=3, default="EUR")
    start_month = models.DateField()
    end_month = models.DateField()
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start_month", "name"]

    def __str__(self):
        return self.name

    @property
    def annualized_yield(self):
        if not self.principal:
            return Decimal("0.00")
        return self.monthly_income * Decimal("12") / self.principal * Decimal("100")


class PrivateLoanReceivable(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="private_loans")
    source_account = models.ForeignKey(
        AssetAccount,
        on_delete=models.SET_NULL,
        related_name="private_loan_receivables",
        blank=True,
        null=True,
        help_text="Cash or savings account that funded the loan and receives interest and principal repayments.",
    )
    name = models.CharField(max_length=140)
    borrower = models.CharField(max_length=140, blank=True)
    current_principal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Amount still owed to you today. Principal repayments reduce this receivable over time.",
    )
    monthly_interest_income = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Optional manual monthly net interest payment (Zins). Used when annual interest rate is 0.",
    )
    annual_interest_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Annual nominal interest rate in percent. If set, LiF calculates monthly net interest automatically.",
    )
    interest_tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("25.00"),
        help_text="Tax rate applied to interest income, in percent.",
    )
    monthly_principal_repayment = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Freely entered monthly principal repayment (Tilgung). Can be 0 if the full principal is repaid at the end.",
    )
    currency = models.CharField(max_length=3, default="EUR")
    disbursement_month = models.DateField(
        blank=True,
        null=True,
        help_text=(
            "Month the money is paid out from the source account. Leave blank if "
            "it was already lent (the principal already counts as a receivable and "
            "the source balance already reflects the outflow). Set a future month "
            "to keep the cash in the source account until then."
        ),
    )
    start_month = models.DateField(blank=True, null=True, help_text="First repayment month. Leave blank if it applies immediately.")
    end_month = models.DateField(blank=True, null=True, help_text="Planned final repayment month. Any remaining principal is repaid in this month.")
    is_gift = models.BooleanField(
        default=False,
        help_text=(
            "Money you will not get back (a gift or donation). It leaves your net worth "
            "when paid out instead of becoming a receivable — no interest or repayment."
        ),
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start_month", "name"]

    def __str__(self):
        return self.name

    def disbursed_before(self, month):
        """True if the loan was already paid out before ``month`` — so the source
        account balance already reflects the outflow and the principal counts as a
        receivable. A blank ``disbursement_month`` means it was lent in the past
        (legacy behaviour). A disbursement in ``month`` or later is modelled as a
        payout event during the projection, not as an opening receivable."""
        if self.disbursement_month is None:
            return True
        return (self.disbursement_month.year, self.disbursement_month.month) < (month.year, month.month)

    @property
    def monthly_cash_in(self):
        return self.starting_monthly_net_interest_income + self.monthly_principal_repayment

    @property
    def starting_monthly_net_interest_income(self):
        return self.monthly_net_interest_for_principal(self.current_principal)

    def monthly_net_interest_for_principal(self, principal):
        if self.annual_interest_rate:
            gross_interest = principal * self.annual_interest_rate / Decimal("100") / Decimal("12")
            tax = gross_interest * self.interest_tax_rate / Decimal("100")
            return (gross_interest - tax).quantize(Decimal("0.01"))
        return self.monthly_interest_income


class RealEstate(models.Model):
    class Use(models.TextChoices):
        RESIDENCE = "residence", "Residence"
        INVESTMENT = "investment", "Investment property"

    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="properties")
    name = models.CharField(max_length=140)
    use = models.CharField(max_length=20, choices=Use.choices, default=Use.RESIDENCE)
    current_value = models.DecimalField(max_digits=12, decimal_places=2)
    annual_appreciation_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=3, default="EUR")
    acquisition_month = models.DateField(blank=True, null=True)
    down_payment = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    acquisition_costs = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    source_account = models.ForeignKey(
        AssetAccount,
        on_delete=models.SET_NULL,
        related_name="funded_properties",
        blank=True,
        null=True,
        help_text="Cash or savings account used for down payment, acquisition costs, and monthly costs.",
    )
    monthly_costs = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Maintenance, insurance, property tax, Hausgeld, and similar monthly carrying costs.",
    )
    saved_monthly_rent = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Residence only. Optional imputed rent saving used to offset a modeled rent expense for rent-vs-buy comparisons.",
    )
    monthly_rent = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    vacancy_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    rent_tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    sale_month = models.DateField(blank=True, null=True)
    sale_costs_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    capital_gains_tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    sale_proceeds_account = models.ForeignKey(
        AssetAccount,
        on_delete=models.SET_NULL,
        related_name="property_sale_proceeds",
        blank=True,
        null=True,
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def acquired_before(self, month):
        if self.acquisition_month is None:
            return True
        return (self.acquisition_month.year, self.acquisition_month.month) < (month.year, month.month)

    @property
    def is_residence(self):
        return self.use == self.Use.RESIDENCE


class RealEstateTransferPlan(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="real_estate_transfer_plans")
    property_item = models.ForeignKey(RealEstate, on_delete=models.CASCADE, related_name="transfer_plans")
    giver = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="given_property_transfer_plans")
    recipient = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="received_property_transfer_plans")
    name = models.CharField(max_length=160)
    transfer_month = models.DateField(help_text="Month when ownership leaves the household planning balance sheet.")
    ownership_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("100.00"),
        help_text="Percent of the property transferred. The first version applies this as a proportional net-worth reduction.",
    )
    taxable_gift_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Planning value counted against the gift allowance after Nießbrauch or other valuation reductions. Enter explicitly.",
    )
    allowance_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("400000.00"),
        help_text="Planning allowance for this giver-recipient relationship in the current 10-year German gift-tax window.",
    )
    allowance_window_years = models.PositiveSmallIntegerField(default=10)
    retained_niessbrauch = models.BooleanField(
        default=False,
        help_text="If enabled, the household keeps the residence saved-rent benefit after the ownership transfer.",
    )
    niessbrauch_annual_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Optional annual value of the retained Nießbrauch used as documentation for the taxable gift value.",
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["transfer_month", "property_item__name", "recipient__name"]

    def __str__(self):
        return self.name

    @property
    def window_start_year(self):
        return self.transfer_month.year - ((self.transfer_month.year - 1) % self.allowance_window_years)

    @property
    def window_end_year(self):
        return self.window_start_year + self.allowance_window_years - 1


class RetirementPlan(models.Model):
    class VehicleType(models.TextChoices):
        STATUTORY = "statutory", "Statutory pension / generic"
        PRIVATE = "private", "Private pension"
        DIREKTVERSICHERUNG = "direktversicherung", "Direktversicherung"
        RIESTER = "riester", "Riester"
        RUERUP = "ruerup", "Ruerup / Basisrente"
        BAV = "bav", "bAV"

    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="retirement_plans")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="retirement_plans")
    name = models.CharField(max_length=140)
    vehicle_type = models.CharField(
        max_length=32,
        choices=VehicleType.choices,
        default=VehicleType.STATUTORY,
        help_text="Planning category for German retirement vehicles. Defaults preserve the current generic net-pension behavior.",
    )
    current_pension_points = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        help_text="Current German statutory pension points, according to Renteninformation.",
    )
    expected_annual_points = models.DecimalField(
        max_digits=5,
        decimal_places=3,
        default=Decimal("1.000"),
        help_text="Assumed yearly pension-point accrual until retirement.",
    )
    pension_value_per_point = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("40.79"),
        help_text="Monthly statutory pension value per point. Keep this editable.",
    )
    private_monthly_pension = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Optional bAV, private pension, Riester, Ruerup, or similar monthly amount.",
    )
    retirement_start_month = models.DateField()
    end_month = models.DateField(blank=True, null=True)
    annual_adjustment_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("1.50"),
        help_text="Assumed yearly pension adjustment in percent after retirement starts.",
    )
    monthly_contribution = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Optional gross monthly contribution before retirement. Leave 0 if already reflected in net salary.",
    )
    contribution_start_month = models.DateField(blank=True, null=True)
    contribution_end_month = models.DateField(
        blank=True,
        null=True,
        help_text="Optional last contribution month. If blank, contributions stop before retirement starts.",
    )
    contribution_relief_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Simple tax/social-security relief on contributions. A 100 EUR contribution with 35% relief costs 65 EUR net cash.",
    )
    payout_taxable_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("100.00"),
        help_text="Share of pension payout exposed to the household pension tax, church tax, and solidarity assumptions.",
    )
    payout_health_insurance_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("100.00"),
        help_text="Share of pension payout exposed to the household health/care insurance assumption.",
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["retirement_start_month", "person__name", "name"]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        errors = {}
        for field_name in ("contribution_relief_rate", "payout_taxable_rate", "payout_health_insurance_rate"):
            value = getattr(self, field_name)
            if value is not None and not (Decimal("0.00") <= value <= Decimal("100.00")):
                errors[field_name] = "Use a percentage between 0 and 100."
        if self.monthly_contribution is not None and self.monthly_contribution < 0:
            errors["monthly_contribution"] = "Contribution cannot be negative."
        if self.contribution_start_month and self.contribution_end_month and self.contribution_end_month < self.contribution_start_month:
            errors["contribution_end_month"] = "Contribution end cannot be before contribution start."
        if errors:
            raise ValidationError(errors)

    @property
    def monthly_pension_from_current_points(self):
        return self.current_pension_points * self.pension_value_per_point

    def payout_deduction_rate(self, household):
        tax_rate = household.pension_tax_rate + household.church_tax_rate + household.solidarity_surcharge_rate
        health_rate = household.health_insurance_rate
        return (
            tax_rate * self.payout_taxable_rate / Decimal("100")
            + health_rate * self.payout_health_insurance_rate / Decimal("100")
        )

    @property
    def contribution_cash_cost(self):
        if self.monthly_contribution <= 0:
            return Decimal("0.00")
        relief = min(max(self.contribution_relief_rate, Decimal("0.00")), Decimal("100.00"))
        return self.monthly_contribution * (Decimal("1.00") - relief / Decimal("100"))


class EquityGrant(models.Model):
    class GrantType(models.TextChoices):
        RSU = "rsu", "RSU"
        STOCK_OPTION = "stock_option", "Stock option"
        BONUS_SHARES = "bonus_shares", "Bonus shares"
        OTHER = "other", "Other"

    class Cadence(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"
        YEARLY = "yearly", "Yearly"

    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="equity_grants")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="equity_grants")
    account = models.ForeignKey(
        AssetAccount,
        on_delete=models.SET_NULL,
        related_name="equity_grants",
        blank=True,
        null=True,
        help_text="Optional cash or savings account where net vesting proceeds land. Blank uses the household default operating account.",
    )
    name = models.CharField(max_length=140)
    grant_type = models.CharField(max_length=30, choices=GrantType.choices, default=GrantType.RSU)
    gross_vest_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Expected gross value for each vesting event in household currency.",
    )
    withholding_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("45.00"),
        help_text="Estimated taxes/social withholding in percent.",
    )
    cadence = models.CharField(max_length=20, choices=Cadence.choices, default=Cadence.QUARTERLY)
    first_vest_month = models.DateField()
    last_vest_month = models.DateField()
    currency = models.CharField(max_length=3, default="EUR")
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["first_vest_month", "person__name", "name"]

    def __str__(self):
        return self.name

    @property
    def net_vest_value(self):
        return self.gross_vest_value * (Decimal("1.00") - (self.withholding_rate / Decimal("100")))


class TrueExpense(models.Model):
    class Cadence(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"
        YEARLY = "yearly", "Yearly"
        ONCE = "once", "Once"

    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="true_expenses")
    account = models.ForeignKey(
        AssetAccount,
        on_delete=models.SET_NULL,
        related_name="true_expenses",
        blank=True,
        null=True,
        help_text="Optional cash or savings account this expense is paid from. Blank uses the household default operating account.",
    )
    name = models.CharField(max_length=140)
    category = models.CharField(max_length=120, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    cadence = models.CharField(max_length=20, choices=Cadence.choices, default=Cadence.YEARLY)
    first_due_month = models.DateField()
    end_month = models.DateField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["first_due_month", "name"]

    def __str__(self):
        return self.name


class ChildMilestone(models.Model):
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="child_milestones")
    name = models.CharField(max_length=140)
    start_month = models.DateField()
    end_month = models.DateField(blank=True, null=True)
    monthly_cost_delta = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    monthly_income_delta = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start_month", "person__name", "name"]

    def __str__(self):
        return self.name


class SalaryChange(models.Model):
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="salary_changes")
    account = models.ForeignKey(
        AssetAccount,
        on_delete=models.SET_NULL,
        related_name="salary_changes",
        blank=True,
        null=True,
        help_text="Optional cash or savings account this salary delta lands in. Blank uses the household default operating account.",
    )
    name = models.CharField(max_length=140)
    start_month = models.DateField()
    end_month = models.DateField(blank=True, null=True)
    monthly_net_income_delta = models.DecimalField(max_digits=12, decimal_places=2)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start_month", "person__name", "name"]

    def __str__(self):
        return self.name


class MoneyRule(models.Model):
    class Kind(models.TextChoices):
        INCOME = "income", "Income"
        EXPENSE = "expense", "Expense"

    class Cadence(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        YEARLY = "yearly", "Yearly"

    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="rules")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="rules", blank=True, null=True)
    account = models.ForeignKey(
        AssetAccount,
        on_delete=models.SET_NULL,
        related_name="money_rules",
        blank=True,
        null=True,
        help_text="Optional cash or savings account this income lands in or this expense is paid from. Blank uses the household default operating account.",
    )
    name = models.CharField(max_length=140)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    cadence = models.CharField(max_length=20, choices=Cadence.choices, default=Cadence.MONTHLY)
    annual_growth_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        blank=True,
        null=True,
        default=None,
        help_text="Optional annual growth applied to this amount. Leave blank to use the household default; set 0 to keep flat.",
    )
    start_month = models.DateField(blank=True, null=True)
    end_month = models.DateField(blank=True, null=True)
    category = models.CharField(max_length=120, blank=True)
    is_taxable = models.BooleanField(
        default=False,
        help_text="For income rules, apply the household income-tax rate (treats the amount as gross). Leave off if the amount is already net.",
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kind", "name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("planner:dashboard")

    @property
    def monthly_amount(self):
        return self.amount if self.cadence == self.Cadence.MONTHLY else self.amount / Decimal("12")


class TransferRule(models.Model):
    class Cadence(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        YEARLY = "yearly", "Yearly"
        ONCE = "once", "Once"

    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="transfer_rules")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="transfer_rules", blank=True, null=True)
    source_account = models.ForeignKey(
        AssetAccount,
        on_delete=models.SET_NULL,
        related_name="outgoing_transfer_rules",
        blank=True,
        null=True,
        help_text="Optional liquid account used as the funding source. Leave blank to use the general cash pool.",
    )
    target_account = models.ForeignKey(AssetAccount, on_delete=models.CASCADE, related_name="incoming_transfer_rules")
    name = models.CharField(max_length=140)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    cadence = models.CharField(max_length=20, choices=Cadence.choices, default=Cadence.MONTHLY)
    start_month = models.DateField(blank=True, null=True)
    end_month = models.DateField(blank=True, null=True)
    category = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["target_account__account_type", "name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("planner:plan_index")

    @property
    def monthly_amount(self):
        if self.cadence == self.Cadence.MONTHLY:
            return self.amount
        if self.cadence == self.Cadence.YEARLY:
            return self.amount / Decimal("12")
        return Decimal("0.00")

    @property
    def signed_monthly_amount(self):
        return self.monthly_amount if self.kind == self.Kind.INCOME else -self.monthly_amount


class FamilyGiftPlan(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="family_gift_plans")
    giver = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="given_gift_plans")
    recipient = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="received_gift_plans")
    source_account = models.ForeignKey(
        AssetAccount,
        on_delete=models.SET_NULL,
        related_name="funded_family_gifts",
        blank=True,
        null=True,
        help_text="Optional cash or savings account funding the gift. Leave blank to use the general liquid pool.",
    )
    target_account = models.ForeignKey(
        AssetAccount,
        on_delete=models.CASCADE,
        related_name="received_family_gifts",
        help_text="Child-owned account receiving the gift, such as a Kinderdepot.",
    )
    name = models.CharField(max_length=160)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    gift_month = models.DateField(help_text="Month when the gift is applied once in the forecast.")
    allowance_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("400000.00"),
        help_text="Planning allowance for this giver-recipient relationship in the current 10-year German gift-tax window.",
    )
    allowance_window_years = models.PositiveSmallIntegerField(
        default=10,
        help_text="German gift-tax planning window. Gifts from the same giver to the same recipient are normally considered over 10 years.",
    )
    purpose = models.CharField(max_length=160, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["gift_month", "recipient__name", "giver__name", "name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("planner:plan_index")

    @property
    def window_start_year(self):
        return self.gift_month.year - ((self.gift_month.year - 1) % self.allowance_window_years)

    @property
    def window_end_year(self):
        return self.window_start_year + self.allowance_window_years - 1


class PlannedInvestmentPurchase(models.Model):
    class AssetType(models.TextChoices):
        ETF = "etf", "ETF"
        STOCK = "stock", "Stock"
        BOND = "bond", "Bond"
        OTHER = "other", "Other"

    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="planned_investment_purchases")
    person = models.ForeignKey(
        Person,
        on_delete=models.CASCADE,
        related_name="planned_investment_purchases",
        blank=True,
        null=True,
    )
    source_account = models.ForeignKey(
        AssetAccount,
        on_delete=models.SET_NULL,
        related_name="funded_investment_purchases",
        blank=True,
        null=True,
        help_text="Optional cash or savings account used to fund the purchase. Leave blank to use the general liquid pool.",
    )
    target_account = models.ForeignKey(
        AssetAccount,
        on_delete=models.CASCADE,
        related_name="planned_investment_purchases",
        help_text="Depot account that will receive the planned purchase.",
    )
    name = models.CharField(max_length=160)
    asset_type = models.CharField(max_length=20, choices=AssetType.choices, default=AssetType.ETF)
    isin = models.CharField(max_length=20, blank=True)
    ticker = models.CharField(max_length=30, blank=True)
    purchase_amount = models.DecimalField(max_digits=12, decimal_places=2)
    purchase_month = models.DateField(help_text="Month when the planned purchase should happen.")
    payout_date = models.DateField(
        blank=True,
        null=True,
        help_text="Optional maturity or payout date, most useful for bonds and target-maturity ETFs.",
    )
    payout_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        blank=True,
        null=True,
        help_text="Optional expected cash payout at maturity. Leave blank to use the purchase amount.",
    )
    annual_distribution_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text=(
            "Expected annual cash distribution/dividend yield in percent for this specific purchase, paid out "
            "(not reinvested) and taxed at the household capital-gains rate. Only used when the target depot is "
            "valued by summed holdings; keep 0 for accumulating funds."
        ),
    )
    distribution_cadence = models.CharField(
        max_length=20,
        choices=AssetAccount.InterestCadence.choices,
        default=AssetAccount.InterestCadence.QUARTERLY,
        help_text="How often this purchase pays its distribution, once acquired.",
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["purchase_month", "name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("planner:plan_index")

    @property
    def expected_payout_amount(self):
        return self.payout_amount if self.payout_amount is not None else self.purchase_amount

# Create your models here.
