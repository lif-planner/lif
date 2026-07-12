from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from planner.feature_flags import FEATURE_FLAG_DEFINITIONS
from planner.models import (
    AssetAccount,
    AssumptionReview,
    BackupEvent,
    CashGoal,
    ChildMilestone,
    Debt,
    DepotHolding,
    EquityGrant,
    FamilyGiftPlan,
    FeatureFlag,
    Household,
    ImportBatch,
    IncomeInvestment,
    MoneyRule,
    MoneyMoneyAccountMapping,
    Person,
    PlannedInvestmentPurchase,
    PrivateLoanReceivable,
    RealEstate,
    RealEstateTransferPlan,
    RetirementPlan,
    SalaryChange,
    Scenario,
    Snapshot,
    SnapshotReview,
    SnapshotReviewAction,
    TransferRule,
    TrueExpense,
)


HOUSEHOLD_NAME = "Demo: German family household"
FIRE_HOUSEHOLD_NAME = "Demo: FIRE depot-draw household"


def add_months(value, months):
    year = value.year + ((value.month - 1 + months) // 12)
    month = ((value.month - 1 + months) % 12) + 1
    return date(year, month, 1)


def demo_snapshot_summary(household, totals, accounts, holdings, counts):
    return {
        "household": {
            "name": household.name,
            "currency": household.currency,
            "start_month": household.start_month.isoformat(),
            "planning_months": household.planning_months,
            "planning_years": household.planning_years,
            "display_granularity": household.display_granularity,
        },
        "totals": totals,
        "counts": counts,
        "accounts": accounts,
        "holdings": holdings,
        "debts": [],
        "private_loans": [],
        "rules": [],
        "cash_goals": [],
    }


class Command(BaseCommand):
    help = "Create a realistic local demo household with German-style income and expenses."

    @transaction.atomic
    def handle(self, *args, **options):
        Household.objects.filter(name__in=[HOUSEHOLD_NAME, FIRE_HOUSEHOLD_NAME]).delete()
        Household.objects.annotate(people_count=Count("people"), rule_count=Count("rules")).filter(
            name__in=["Home", "My household"],
            people_count=0,
            rule_count=0,
        ).delete()

        household = Household.objects.create(
            name=HOUSEHOLD_NAME,
            data_mode=Household.DataMode.DEMO,
            starting_balance=Decimal("18500.00"),
            start_month=date(2026, 6, 1),
            planning_months=36,
            planning_years=40,
            display_granularity=Household.DisplayGranularity.AUTO,
            annual_inflation_rate=Decimal("2.00"),
            default_income_growth_rate=Decimal("2.00"),
            pension_tax_rate=Decimal("18.00"),
            capital_gains_tax_rate=Decimal("25.00"),
            vorabpauschale_basiszins_rate=Decimal("3.20"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
            health_insurance_rate=Decimal("11.00"),
            emergency_fund_months=Decimal("6.00"),
            currency="EUR",
        )

        people = {
            "alex": Person.objects.create(
                household=household,
                name="Alex",
                role=Person.Role.ADULT,
                birth_date=date(1986, 4, 12),
                notes="Adult with a full-time net salary.",
            ),
            "sam": Person.objects.create(
                household=household,
                name="Sam",
                role=Person.Role.ADULT,
                birth_date=date(1985, 9, 3),
                notes="Adult with a part-time net salary.",
            ),
            "lina": Person.objects.create(
                household=household,
                name="Lina",
                role=Person.Role.CHILD,
                birth_date=date(2016, 2, 18),
                notes="Ten-year-old child with Kindergeld income and school-age costs.",
            ),
            "noah": Person.objects.create(
                household=household,
                name="Noah",
                role=Person.Role.CHILD,
                birth_date=date(2018, 7, 9),
                notes="Eight-year-old child with Kindergeld income and school-age costs.",
            ),
        }

        accounts = {
            "giro": AssetAccount.objects.create(
                household=household,
                name="Girokonto",
                account_type=AssetAccount.AccountType.CASH,
                balance=Decimal("6500.00"),
                currency="EUR",
                source=AssetAccount.Source.MANUAL,
                institution="House bank",
                as_of_date=household.start_month,
                notes="Demo liquid checking balance.",
            ),
            "tagesgeld": AssetAccount.objects.create(
                household=household,
                name="Tagesgeld",
                account_type=AssetAccount.AccountType.SAVINGS,
                balance=Decimal("12000.00"),
                savings_annual_interest_rate=Decimal("2.50"),
                savings_interest_cadence=AssetAccount.InterestCadence.MONTHLY,
                savings_interest_tax_rate=Decimal("25.00"),
                currency="EUR",
                source=AssetAccount.Source.MANUAL,
                institution="Savings bank",
                as_of_date=household.start_month,
                notes="Demo emergency fund.",
            ),
            "depot": AssetAccount.objects.create(
                household=household,
                name="ETF Depot",
                account_type=AssetAccount.AccountType.DEPOT,
                balance=Decimal("50000.00"),
                depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
                depot_teilfreistellung_rate=Decimal("30.00"),
                depot_vorabpauschale_enabled=True,
                currency="EUR",
                source=AssetAccount.Source.MANUAL,
                institution="Broker",
                as_of_date=household.start_month,
                notes=(
                    "Demo depot value from summed holdings; each holding models its own "
                    "distribution yield and cadence directly. This can later come from "
                    "YNAB or manual snapshots."
                ),
            ),
            "lina_kinderdepot": AssetAccount.objects.create(
                household=household,
                name="Lina Kinderdepot",
                account_type=AssetAccount.AccountType.DEPOT,
                owner_type=AssetAccount.OwnerType.PERSON,
                owner_person=people["lina"],
                counts_in_household_net_worth=False,
                balance=Decimal("2500.00"),
                depot_annual_return_rate=Decimal("5.00"),
                currency="EUR",
                source=AssetAccount.Source.MANUAL,
                institution="Junior broker",
                as_of_date=household.start_month,
                notes="Demo child-owned depot. Tracked for family visibility, but excluded from household retirement net worth.",
            ),
            "home": AssetAccount.objects.create(
                household=household,
                name="Owner-occupied apartment",
                account_type=AssetAccount.AccountType.OTHER,
                balance=Decimal("0.00"),
                currency="EUR",
                source=AssetAccount.Source.MANUAL,
                institution="Manual estimate",
                as_of_date=household.start_month,
                notes="Demo placeholder account. The RealEstate model carries the residence value to avoid double-counting.",
            ),
            "mortgage_a": AssetAccount.objects.create(
                household=household,
                name="Mortgage tranche A",
                account_type=AssetAccount.AccountType.LOAN,
                balance=Decimal("210000.00"),
                currency="EUR",
                source=AssetAccount.Source.MANUAL,
                institution="Mortgage bank",
                as_of_date=household.start_month,
                notes="Demo mortgage liability with longer fixed interest period.",
            ),
            "mortgage_b": AssetAccount.objects.create(
                household=household,
                name="Mortgage tranche B",
                account_type=AssetAccount.AccountType.LOAN,
                balance=Decimal("110000.00"),
                currency="EUR",
                source=AssetAccount.Source.MANUAL,
                institution="Mortgage bank",
                as_of_date=household.start_month,
                notes="Demo mortgage liability with shorter fixed interest period.",
            ),
        }
        household.default_operating_account = accounts["giro"]
        household.save(update_fields=["default_operating_account", "updated_at"])

        FamilyGiftPlan.objects.create(
            household=household,
            name="Lina Kinderdepot first gift",
            giver=people["alex"],
            recipient=people["lina"],
            source_account=accounts["tagesgeld"],
            target_account=accounts["lina_kinderdepot"],
            amount=Decimal("10000.00"),
            gift_month=date(2027, 1, 1),
            purpose="Start long-term child depot investing",
            notes="Demo lifetime gift. Leaves household planning net worth and lands in Lina's tracked Kinderdepot.",
        )
        FamilyGiftPlan.objects.create(
            household=household,
            name="Lina next allowance-window gift",
            giver=people["sam"],
            recipient=people["lina"],
            source_account=accounts["tagesgeld"],
            target_account=accounts["lina_kinderdepot"],
            amount=Decimal("15000.00"),
            gift_month=date(2037, 1, 1),
            purpose="Use a later 10-year gift-tax planning window",
            notes="Demo second parent/child gift in a later window.",
        )

        mortgage_a = Debt.objects.create(
            household=household,
            account=accounts["mortgage_a"],
            name="Mortgage tranche A repayment",
            current_principal=Decimal("210000.00"),
            annual_interest_rate=Decimal("2.85"),
            monthly_payment=Decimal("950.00"),
            start_month=household.start_month,
            fixed_interest_until=date(2036, 5, 1),
            refinance_annual_interest_rate=Decimal("4.25"),
            refinance_monthly_payment=Decimal("1180.00"),
            notes="Demo annuity mortgage tranche. Projection splits each payment into interest and principal.",
        )
        mortgage_b = Debt.objects.create(
            household=household,
            account=accounts["mortgage_b"],
            name="Mortgage tranche B repayment",
            current_principal=Decimal("110000.00"),
            annual_interest_rate=Decimal("4.10"),
            monthly_payment=Decimal("620.00"),
            start_month=household.start_month,
            fixed_interest_until=date(2031, 5, 1),
            refinance_annual_interest_rate=Decimal("4.75"),
            refinance_monthly_payment=Decimal("700.00"),
            notes="Second demo mortgage tranche with a shorter fixed-interest period.",
        )
        residence = RealEstate.objects.create(
            household=household,
            name="Owner-occupied apartment",
            use=RealEstate.Use.RESIDENCE,
            current_value=Decimal("520000.00"),
            annual_appreciation_rate=Decimal("1.50"),
            currency="EUR",
            source_account=accounts["giro"],
            monthly_costs=Decimal("520.00"),
            saved_monthly_rent=Decimal("1600.00"),
            notes="Demo residence with saved rent for rent-vs-buy comparison. Keep comparable rent as an expense if you want this to offset it.",
        )
        mortgage_a.real_estate = residence
        mortgage_a.save(update_fields=["real_estate"])
        mortgage_b.real_estate = residence
        mortgage_b.save(update_fields=["real_estate"])
        RealEstateTransferPlan.objects.create(
            household=household,
            property_item=residence,
            giver=people["alex"],
            recipient=people["lina"],
            name="Gift residence to Lina with Nießbrauch",
            transfer_month=date(2046, 1, 1),
            ownership_percent=Decimal("100.00"),
            taxable_gift_value=Decimal("300000.00"),
            retained_niessbrauch=True,
            niessbrauch_annual_value=Decimal("19200.00"),
            notes=(
                "Demo vorweggenommene Erbfolge: ownership leaves the parents' planning "
                "net worth, while retained Nießbrauch keeps the saved-rent living benefit."
            ),
        )

        rental_mortgage_account = AssetAccount.objects.create(
            household=household,
            name="Rental flat mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("95000.00"),
            currency="EUR",
            source=AssetAccount.Source.MANUAL,
            institution="Mortgage bank",
            as_of_date=household.start_month,
            notes="Demo mortgage financing the rental investment property.",
        )
        rental_mortgage = Debt.objects.create(
            household=household,
            account=rental_mortgage_account,
            name="Rental flat mortgage repayment",
            current_principal=Decimal("95000.00"),
            annual_interest_rate=Decimal("3.20"),
            monthly_payment=Decimal("480.00"),
            start_month=household.start_month,
            notes="Demo rental-property mortgage; paid off automatically when the property sells.",
        )
        rental_property = RealEstate.objects.create(
            household=household,
            name="Rental flat in Leipzig",
            use=RealEstate.Use.INVESTMENT,
            current_value=Decimal("165000.00"),
            annual_appreciation_rate=Decimal("1.80"),
            currency="EUR",
            source_account=accounts["giro"],
            monthly_costs=Decimal("180.00"),
            monthly_rent=Decimal("780.00"),
            vacancy_rate=Decimal("5.00"),
            rent_tax_rate=Decimal("30.00"),
            sale_month=date(2033, 6, 1),
            sale_costs_rate=Decimal("6.00"),
            capital_gains_tax_rate=Decimal("25.00"),
            sale_proceeds_account=accounts["tagesgeld"],
            notes=(
                "Demo investment property: net rental income after vacancy and rent tax, "
                "monthly carrying costs, a linked mortgage, and a planned 2033 sale that pays "
                "off the mortgage and lands net proceeds in Tagesgeld."
            ),
        )
        rental_mortgage.real_estate = rental_property
        rental_mortgage.save(update_fields=["real_estate"])

        holdings = [
            (
                "iShares Core MSCI World UCITS ETF Acc",
                "IE00B4L5Y983",
                "EUNL",
                "ETF accumulating",
                "180.000000",
                "100.00",
                None,
                None,
                Decimal("0.00"),
                AssetAccount.InterestCadence.QUARTERLY,
                "Accumulating ETF; distributions are reinvested inside the fund.",
            ),
            (
                "Vanguard FTSE All-World UCITS ETF Dist",
                "IE00B3RBWM25",
                "VWRL",
                "Bond ETF distributing",
                "90.000000",
                "100.00",
                None,
                None,
                Decimal("1.80"),
                AssetAccount.InterestCadence.QUARTERLY,
                "Distributing all-world equity ETF; models its own quarterly distribution yield.",
            ),
            (
                "iShares Core MSCI EM IMI UCITS ETF Acc",
                "IE00BKM4GZ66",
                "IS3N",
                "ETF accumulating",
                "120.000000",
                "50.00",
                None,
                None,
                Decimal("0.00"),
                AssetAccount.InterestCadence.QUARTERLY,
                "Accumulating emerging-markets ETF.",
            ),
            (
                "iShares Global Corp Bond UCITS ETF Dist",
                "IE00B9M6RS56",
                "CORP",
                "ETF distributing",
                "100.000000",
                "40.00",
                None,
                None,
                Decimal("3.20"),
                AssetAccount.InterestCadence.QUARTERLY,
                "Distributing bond ETF; models its own quarterly distribution yield directly.",
            ),
            (
                "iShares iBonds target maturity bond ETF",
                "IE0008UEVOE0",
                "",
                "Bond target maturity",
                "100.000000",
                "10.00",
                date(2028, 12, 31),
                "1100.00",
                Decimal("0.00"),
                AssetAccount.InterestCadence.QUARTERLY,
                "Example target-maturity bond holding with an expected payout date.",
            ),
            (
                "Microsoft",
                "US5949181045",
                "MSFT",
                "Stock accumulating-like",
                "20.000000",
                "350.00",
                None,
                None,
                Decimal("0.00"),
                AssetAccount.InterestCadence.QUARTERLY,
                "Individual stock; dividend ignored in demo because growth is the main assumption.",
            ),
            (
                "Allianz",
                "DE0008404005",
                "ALV",
                "Stock distributing",
                "20.000000",
                "250.00",
                None,
                None,
                Decimal("4.00"),
                AssetAccount.InterestCadence.YEARLY,
                "Dividend stock; German blue chips typically pay once a year, modeled directly on the holding.",
            ),
            (
                "SPDR MSCI World Small Cap UCITS ETF",
                "IE00BCBJG560",
                "ZPRV",
                "ETF accumulating",
                "0.000000",
                "50.00",
                None,
                None,
                Decimal("0.00"),
                AssetAccount.InterestCadence.QUARTERLY,
                "Small-cap ETF allocation.",
            ),
        ]

        for (
            name, isin, ticker, asset_class, quantity, latest_price, payout_date, payout_amount,
            distribution_rate, distribution_cadence, notes,
        ) in holdings:
            DepotHolding.objects.create(
                asset_account=accounts["depot"],
                name=name,
                isin=isin,
                ticker=ticker,
                asset_class=asset_class,
                quantity=Decimal(quantity),
                latest_price=Decimal(latest_price),
                currency="EUR",
                as_of_date=household.start_month,
                payout_date=payout_date,
                payout_amount=Decimal(payout_amount) if payout_amount else None,
                annual_distribution_rate=distribution_rate,
                distribution_cadence=distribution_cadence,
                notes=notes,
            )

        IncomeInvestment.objects.create(
            household=household,
            name="Solar park participation",
            investment_type=IncomeInvestment.InvestmentType.SOLAR,
            principal=Decimal("18000.00"),
            monthly_income=Decimal("145.00"),
            annual_growth_rate=Decimal("1.00"),
            currency="EUR",
            start_month=date(2027, 1, 1),
            end_month=date(2028, 12, 1),
            notes="Demo solar investment with a small annual income-growth assumption and a defined end date.",
        )
        PrivateLoanReceivable.objects.create(
            household=household,
            source_account=accounts["tagesgeld"],
            name="Family bridge loan",
            borrower="Sibling",
            current_principal=Decimal("6000.00"),
            annual_interest_rate=Decimal("4.00"),
            interest_tax_rate=Decimal("25.00"),
            monthly_principal_repayment=Decimal("250.00"),
            currency="EUR",
            start_month=date(2026, 7, 1),
            end_month=date(2028, 6, 1),
            notes="Demo private family loan receivable: Zins is income, Tilgung and the final repayment return to Tagesgeld.",
        )
        PrivateLoanReceivable.objects.create(
            household=household,
            source_account=accounts["tagesgeld"],
            name="Future family startup loan",
            borrower="Cousin",
            current_principal=Decimal("10000.00"),
            annual_interest_rate=Decimal("3.50"),
            interest_tax_rate=Decimal("25.00"),
            monthly_principal_repayment=Decimal("0.00"),
            currency="EUR",
            disbursement_month=date(2027, 4, 1),
            start_month=date(2027, 5, 1),
            end_month=date(2030, 4, 1),
            notes="Demo future-disbursed private loan: cash leaves Tagesgeld in 2027, interest is income, and principal returns at the end.",
        )

        RetirementPlan.objects.create(
            household=household,
            person=people["alex"],
            name="Alex statutory pension assumption",
            current_pension_points=Decimal("23.400"),
            expected_annual_points=Decimal("1.100"),
            pension_value_per_point=Decimal("40.79"),
            private_monthly_pension=Decimal("250.00"),
            retirement_start_month=date(2053, 5, 1),
            annual_adjustment_rate=Decimal("1.50"),
            notes="Demo German statutory pension assumption based on Rentenpunkte. Replace with Renteninformation values.",
        )
        RetirementPlan.objects.create(
            household=household,
            person=people["sam"],
            name="Sam statutory pension assumption",
            current_pension_points=Decimal("18.750"),
            expected_annual_points=Decimal("0.800"),
            pension_value_per_point=Decimal("40.79"),
            private_monthly_pension=Decimal("150.00"),
            retirement_start_month=date(2052, 10, 1),
            annual_adjustment_rate=Decimal("1.50"),
            notes="Demo German statutory pension assumption for a part-time earner.",
        )
        RetirementPlan.objects.create(
            household=household,
            person=people["alex"],
            name="Alex bAV Direktversicherung",
            vehicle_type=RetirementPlan.VehicleType.BAV,
            current_pension_points=Decimal("0.000"),
            expected_annual_points=Decimal("0.000"),
            pension_value_per_point=Decimal("40.79"),
            private_monthly_pension=Decimal("350.00"),
            retirement_start_month=date(2053, 5, 1),
            annual_adjustment_rate=Decimal("1.00"),
            monthly_contribution=Decimal("250.00"),
            contribution_start_month=household.start_month,
            contribution_relief_rate=Decimal("35.00"),
            payout_taxable_rate=Decimal("100.00"),
            payout_health_insurance_rate=Decimal("100.00"),
            notes="Demo bAV/Direktversicherung style plan: contribution cash cost and payout exposure are simplified assumptions.",
        )

        EquityGrant.objects.create(
            household=household,
            person=people["alex"],
            account=accounts["giro"],
            name="Alex RSU grant 2026",
            grant_type=EquityGrant.GrantType.RSU,
            gross_vest_value=Decimal("4200.00"),
            withholding_rate=Decimal("47.00"),
            cadence=EquityGrant.Cadence.QUARTERLY,
            first_vest_month=date(2026, 9, 1),
            last_vest_month=date(2028, 6, 1),
            currency="EUR",
            notes="Demo RSU grant. Net vesting income is estimated after withholding.",
        )

        Scenario.objects.create(
            household=household,
            name="Sam unpaid sabbatical year",
            monthly_income_delta=Decimal("-2400.00"),
            monthly_expense_delta=Decimal("-250.00"),
            notes="Simple scenario: Sam stops earning for a year-like planning stress test.",
        )
        Scenario.objects.create(
            household=household,
            name="Higher mortgage refinance stress",
            monthly_expense_delta=Decimal("450.00"),
            notes="Simple scenario: household absorbs higher financing costs.",
        )

        CashGoal.objects.create(
            household=household,
            name="Family annual cash need",
            annual_amount=Decimal("30000.00"),
            indexed_to_inflation=True,
            start_year=2026,
            end_year=2051,
            notes="Demo FIRE-style yearly cash need while both adults are before retirement.",
        )
        CashGoal.objects.create(
            household=household,
            name="Retirement annual cash need",
            annual_amount=Decimal("36000.00"),
            indexed_to_inflation=True,
            start_year=2052,
            notes="Demo retirement cash need to compare pensions and other income against a portfolio draw.",
        )

        TrueExpense.objects.create(
            household=household,
            account=accounts["giro"],
            name="Annual insurance bundle",
            category="Insurance",
            amount=Decimal("950.00"),
            cadence=TrueExpense.Cadence.YEARLY,
            first_due_month=household.start_month,
            notes="Demo annual true expense.",
        )
        TrueExpense.objects.create(
            household=household,
            account=accounts["giro"],
            name="Vacation budget",
            category="Travel",
            amount=Decimal("4200.00"),
            cadence=TrueExpense.Cadence.YEARLY,
            first_due_month=date(2026, 7, 1),
            notes="Demo annual holiday cost.",
        )
        TrueExpense.objects.create(
            household=household,
            account=accounts["giro"],
            name="Gifts and holidays",
            category="True expenses",
            amount=Decimal("1400.00"),
            cadence=TrueExpense.Cadence.YEARLY,
            first_due_month=date(2026, 12, 1),
            notes="Demo yearly gifts and holiday buffer.",
        )
        TrueExpense.objects.create(
            household=household,
            account=accounts["giro"],
            name="Nebenkosten back payment buffer",
            category="Housing",
            amount=Decimal("900.00"),
            cadence=TrueExpense.Cadence.YEARLY,
            first_due_month=date(2027, 3, 1),
            notes="Demo owner/running-cost back-payment buffer.",
        )

        ChildMilestone.objects.create(
            person=people["lina"],
            name="Secondary school costs",
            start_month=date(2026, 9, 1),
            monthly_cost_delta=Decimal("85.00"),
            notes="Demo school phase with extra materials, trips, and activities.",
        )
        ChildMilestone.objects.create(
            person=people["noah"],
            name="Sports club and equipment",
            start_month=date(2027, 1, 1),
            monthly_cost_delta=Decimal("60.00"),
            notes="Demo recurring activity cost.",
        )

        SalaryChange.objects.create(
            person=people["sam"],
            account=accounts["giro"],
            name="Temporary 80 percent work",
            start_month=date(2027, 1, 1),
            end_month=date(2027, 12, 1),
            monthly_net_income_delta=Decimal("-480.00"),
            notes="Demo part-time year.",
        )
        SalaryChange.objects.create(
            person=people["alex"],
            account=accounts["giro"],
            name="Expected raise",
            start_month=date(2028, 1, 1),
            monthly_net_income_delta=Decimal("250.00"),
            notes="Demo salary increase.",
        )

        rules = [
            ("Net salary Alex", MoneyRule.Kind.INCOME, "3200.00", MoneyRule.Cadence.MONTHLY, people["alex"], "Salary"),
            ("Net salary Sam", MoneyRule.Kind.INCOME, "2400.00", MoneyRule.Cadence.MONTHLY, people["sam"], "Salary"),
            ("Kindergeld Lina", MoneyRule.Kind.INCOME, "255.00", MoneyRule.Cadence.MONTHLY, people["lina"], "Benefits"),
            ("Kindergeld Noah", MoneyRule.Kind.INCOME, "255.00", MoneyRule.Cadence.MONTHLY, people["noah"], "Benefits"),
            ("Hausgeld and maintenance", MoneyRule.Kind.EXPENSE, "520.00", MoneyRule.Cadence.MONTHLY, None, "Housing"),
            ("Property tax and owner costs", MoneyRule.Kind.EXPENSE, "90.00", MoneyRule.Cadence.MONTHLY, None, "Housing"),
            ("Electricity", MoneyRule.Kind.EXPENSE, "95.00", MoneyRule.Cadence.MONTHLY, None, "Utilities"),
            ("Internet and mobile", MoneyRule.Kind.EXPENSE, "85.00", MoneyRule.Cadence.MONTHLY, None, "Utilities"),
            ("Groceries and household", MoneyRule.Kind.EXPENSE, "1050.00", MoneyRule.Cadence.MONTHLY, None, "Living"),
            ("Restaurants and cafes", MoneyRule.Kind.EXPENSE, "220.00", MoneyRule.Cadence.MONTHLY, None, "Living"),
            ("Deutschlandtickets", MoneyRule.Kind.EXPENSE, "116.00", MoneyRule.Cadence.MONTHLY, None, "Transport"),
            ("School and activities Lina", MoneyRule.Kind.EXPENSE, "180.00", MoneyRule.Cadence.MONTHLY, people["lina"], "Child"),
            ("School and activities Noah", MoneyRule.Kind.EXPENSE, "160.00", MoneyRule.Cadence.MONTHLY, people["noah"], "Child"),
            ("Clothing", MoneyRule.Kind.EXPENSE, "240.00", MoneyRule.Cadence.MONTHLY, None, "Living"),
            ("Health extras and medication", MoneyRule.Kind.EXPENSE, "90.00", MoneyRule.Cadence.MONTHLY, None, "Health"),
        ]

        for name, kind, amount, cadence, person, category in rules:
            MoneyRule.objects.create(
                household=household,
                person=person,
                name=name,
                kind=kind,
                amount=Decimal(amount),
                cadence=cadence,
                category=category,
                start_month=household.start_month if cadence == MoneyRule.Cadence.YEARLY else None,
                notes="Demo data. Replace with your own actual numbers.",
            )
        MoneyRule.objects.create(
            household=household,
            person=people["alex"],
            account=accounts["giro"],
            name="Alex annual bonus",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("20000.00"),
            cadence=MoneyRule.Cadence.YEARLY,
            category="Bonus",
            start_month=date(2026, 12, 1),
            notes="Demo yearly net bonus paid in December.",
        )

        transfer_rules = [
            ("ETF savings plan", "850.00", TransferRule.Cadence.MONTHLY, None, accounts["tagesgeld"], accounts["depot"], "Investing"),
            ("One-time ETF top-up", "12000.00", TransferRule.Cadence.ONCE, None, accounts["tagesgeld"], accounts["depot"], "Investing"),
            ("Emergency buffer contribution", "300.00", TransferRule.Cadence.MONTHLY, None, accounts["giro"], accounts["tagesgeld"], "Savings"),
        ]
        for name, amount, cadence, person, source_account, target_account, category in transfer_rules:
            TransferRule.objects.create(
                household=household,
                person=person,
                source_account=source_account,
                target_account=target_account,
                name=name,
                amount=Decimal(amount),
                cadence=cadence,
                category=category,
                start_month=add_months(household.start_month, 12) if cadence == TransferRule.Cadence.ONCE else household.start_month if cadence == TransferRule.Cadence.YEARLY else None,
                notes="Demo data. Replace with your own actual numbers.",
            )

        PlannedInvestmentPurchase.objects.create(
            household=household,
            source_account=accounts["tagesgeld"],
            target_account=accounts["depot"],
            name="Planned 2027 bond ladder buy",
            asset_type=PlannedInvestmentPurchase.AssetType.BOND,
            isin="IE0008UEVOE0",
            purchase_amount=Decimal("28450.00"),
            purchase_month=date(2027, 12, 1),
            payout_date=date(2029, 1, 1),
            payout_amount=Decimal("30000.00"),
            notes="Demo future bond purchase: cash leaves Tagesgeld in 2027 and returns as a maturity payout in 2029.",
        )

        MoneyMoneyAccountMapping.objects.create(
            household=household,
            account_name="MoneyMoney Girokonto",
            account_type=AssetAccount.AccountType.CASH,
            notes="Demo override: map a MoneyMoney checking account to LiF cash.",
        )
        MoneyMoneyAccountMapping.objects.create(
            household=household,
            account_name="MoneyMoney Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            notes="Demo override: keep savings accounts distinct from checking cash.",
        )
        MoneyMoneyAccountMapping.objects.create(
            household=household,
            account_name="MoneyMoney ETF Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            notes="Demo override: map a MoneyMoney portfolio to a LiF depot.",
        )

        ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.MONEYMONEY,
            status=ImportBatch.Status.APPLIED,
            filename="demo_moneymoney_accounts",
            row_count=6,
            valid_count=6,
            error_count=0,
            summary={
                "import_kind": "moneymoney_accounts",
                "apply_result": {"created_count": 0, "updated_count": 6, "skipped_count": 0},
                "notes": "Demo batch showing a successful local MoneyMoney account import.",
            },
            notes="Demo import history only; no external data was accessed.",
        )
        ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.CSV_DEPOT_HOLDINGS,
            status=ImportBatch.Status.APPLIED,
            filename="demo_depot_holdings.csv",
            row_count=len(holdings),
            valid_count=len(holdings),
            error_count=0,
            summary={
                "import_kind": "depot_holdings",
                "apply_result": {"created_count": len(holdings), "updated_count": 0, "skipped_count": 0},
                "notes": "Demo batch showing a successful depot holdings import.",
            },
            notes="Demo import history for the holdings already seeded above.",
        )

        BackupEvent.objects.filter(
            filename__in=["demo-20260626-120000-manual.sqlite3", "demo-restore-candidate.sqlite3"]
        ).delete()
        BackupEvent.objects.create(
            action=BackupEvent.Action.BACKUP,
            status=BackupEvent.Status.SUCCEEDED,
            filename="demo-20260626-120000-manual.sqlite3",
            detail="Demo manual backup event. Create a real backup before editing real data.",
        )
        BackupEvent.objects.create(
            action=BackupEvent.Action.RESTORE,
            status=BackupEvent.Status.FAILED,
            filename="demo-restore-candidate.sqlite3",
            detail="Demo failed restore event: restore confirmation checkbox was not selected.",
        )

        for key in ["snapshots", "moneymoney_import"]:
            FeatureFlag.objects.update_or_create(
                key=key,
                defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS[key]["description"]},
            )

        snapshot_counts = {
            "people": 4,
            "accounts": 6,
            "holdings": 5,
            "debts": 2,
            "private_loans": 1,
            "rules": len(rules),
            "transfer_rules": len(transfer_rules),
            "cash_goals": 2,
        }
        baseline_snapshot = Snapshot.objects.create(
            household=household,
            name="Annual review baseline 2025",
            snapshot_date=date(2025, 1, 1),
            notes="Demo frozen baseline for testing the Annual Review workflow.",
            summary=demo_snapshot_summary(
                household,
                {
                    "liquid": "14000.00",
                    "invested": "41000.00",
                    "other_assets": "500000.00",
                    "liabilities": "340000.00",
                    "net_worth": "215000.00",
                },
                [
                    {
                        "name": "Girokonto",
                        "type": AssetAccount.AccountType.CASH,
                        "balance": "5000.00",
                        "effective_balance": "5000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-01-01",
                    },
                    {
                        "name": "Tagesgeld",
                        "type": AssetAccount.AccountType.SAVINGS,
                        "balance": "9000.00",
                        "effective_balance": "9000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-01-01",
                    },
                    {
                        "name": "ETF Depot",
                        "type": AssetAccount.AccountType.DEPOT,
                        "balance": "41000.00",
                        "effective_balance": "41000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-01-01",
                    },
                    {
                        "name": "Owner-occupied apartment",
                        "type": AssetAccount.AccountType.OTHER,
                        "balance": "500000.00",
                        "effective_balance": "500000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-01-01",
                    },
                    {
                        "name": "Mortgage tranche A",
                        "type": AssetAccount.AccountType.LOAN,
                        "balance": "225000.00",
                        "effective_balance": "225000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-01-01",
                    },
                    {
                        "name": "Mortgage tranche B",
                        "type": AssetAccount.AccountType.LOAN,
                        "balance": "115000.00",
                        "effective_balance": "115000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-01-01",
                    },
                ],
                [
                    {
                        "account": "ETF Depot",
                        "name": "iShares Core MSCI World UCITS ETF Acc",
                        "isin": "IE00B4L5Y983",
                        "ticker": "EUNL",
                        "asset_class": "ETF accumulating",
                        "quantity": "160.000000",
                        "latest_price": "95.00",
                        "current_value": "15200.00",
                        "currency": "EUR",
                        "as_of_date": "2025-01-01",
                        "payout_date": "",
                    },
                    {
                        "account": "ETF Depot",
                        "name": "Vanguard FTSE All-World UCITS ETF Dist",
                        "isin": "IE00B3RBWM25",
                        "ticker": "VWRL",
                        "asset_class": "ETF distributing",
                        "quantity": "75.000000",
                        "latest_price": "96.00",
                        "current_value": "7200.00",
                        "currency": "EUR",
                        "as_of_date": "2025-01-01",
                        "payout_date": "",
                    },
                    {
                        "account": "ETF Depot",
                        "name": "iShares Core MSCI EM IMI UCITS ETF Acc",
                        "isin": "IE00BKM4GZ66",
                        "ticker": "IS3N",
                        "asset_class": "ETF accumulating",
                        "quantity": "100.000000",
                        "latest_price": "48.00",
                        "current_value": "4800.00",
                        "currency": "EUR",
                        "as_of_date": "2025-01-01",
                        "payout_date": "",
                    },
                    {
                        "account": "ETF Depot",
                        "name": "iShares Global Corp Bond UCITS ETF Dist",
                        "isin": "IE00B9M6RS56",
                        "ticker": "CORP",
                        "asset_class": "ETF distributing",
                        "quantity": "100.000000",
                        "latest_price": "38.00",
                        "current_value": "3800.00",
                        "currency": "EUR",
                        "as_of_date": "2025-01-01",
                        "payout_date": "",
                    },
                    {
                        "account": "ETF Depot",
                        "name": "Microsoft",
                        "isin": "US5949181045",
                        "ticker": "MSFT",
                        "asset_class": "Stock accumulating-like",
                        "quantity": "20.000000",
                        "latest_price": "500.00",
                        "current_value": "10000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-01-01",
                        "payout_date": "",
                    },
                ],
                snapshot_counts,
            ),
        )
        comparison_snapshot = Snapshot.objects.create(
            household=household,
            name="Annual review result 2025",
            snapshot_date=date(2025, 12, 31),
            notes="Demo review result: cash grew, depot appreciated, and mortgages were repaid.",
            summary=demo_snapshot_summary(
                household,
                {
                    "liquid": "18500.00",
                    "invested": "50000.00",
                    "other_assets": "520000.00",
                    "liabilities": "320000.00",
                    "net_worth": "268500.00",
                },
                [
                    {
                        "name": "Girokonto",
                        "type": AssetAccount.AccountType.CASH,
                        "balance": "6500.00",
                        "effective_balance": "6500.00",
                        "currency": "EUR",
                        "as_of_date": "2025-12-31",
                    },
                    {
                        "name": "Tagesgeld",
                        "type": AssetAccount.AccountType.SAVINGS,
                        "balance": "12000.00",
                        "effective_balance": "12000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-12-31",
                    },
                    {
                        "name": "ETF Depot",
                        "type": AssetAccount.AccountType.DEPOT,
                        "balance": "50000.00",
                        "effective_balance": "50000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-12-31",
                    },
                    {
                        "name": "Owner-occupied apartment",
                        "type": AssetAccount.AccountType.OTHER,
                        "balance": "520000.00",
                        "effective_balance": "520000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-12-31",
                    },
                    {
                        "name": "Mortgage tranche A",
                        "type": AssetAccount.AccountType.LOAN,
                        "balance": "210000.00",
                        "effective_balance": "210000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-12-31",
                    },
                    {
                        "name": "Mortgage tranche B",
                        "type": AssetAccount.AccountType.LOAN,
                        "balance": "110000.00",
                        "effective_balance": "110000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-12-31",
                    },
                ],
                [
                    {
                        "account": "ETF Depot",
                        "name": "iShares Core MSCI World UCITS ETF Acc",
                        "isin": "IE00B4L5Y983",
                        "ticker": "EUNL",
                        "asset_class": "ETF accumulating",
                        "quantity": "180.000000",
                        "latest_price": "100.00",
                        "current_value": "18000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-12-31",
                        "payout_date": "",
                    },
                    {
                        "account": "ETF Depot",
                        "name": "Vanguard FTSE All-World UCITS ETF Dist",
                        "isin": "IE00B3RBWM25",
                        "ticker": "VWRL",
                        "asset_class": "ETF distributing",
                        "quantity": "90.000000",
                        "latest_price": "100.00",
                        "current_value": "9000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-12-31",
                        "payout_date": "",
                    },
                    {
                        "account": "ETF Depot",
                        "name": "iShares Core MSCI EM IMI UCITS ETF Acc",
                        "isin": "IE00BKM4GZ66",
                        "ticker": "IS3N",
                        "asset_class": "ETF accumulating",
                        "quantity": "120.000000",
                        "latest_price": "50.00",
                        "current_value": "6000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-12-31",
                        "payout_date": "",
                    },
                    {
                        "account": "ETF Depot",
                        "name": "iShares Global Corp Bond UCITS ETF Dist",
                        "isin": "IE00B9M6RS56",
                        "ticker": "CORP",
                        "asset_class": "ETF distributing",
                        "quantity": "100.000000",
                        "latest_price": "40.00",
                        "current_value": "4000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-12-31",
                        "payout_date": "",
                    },
                    {
                        "account": "ETF Depot",
                        "name": "iShares iBonds target maturity bond ETF",
                        "isin": "IE0008UEVOE0",
                        "ticker": "",
                        "asset_class": "Bond target maturity",
                        "quantity": "100.000000",
                        "latest_price": "10.00",
                        "current_value": "1000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-12-31",
                        "payout_date": "2028-12-31",
                        "payout_amount": "1100.00",
                    },
                    {
                        "account": "ETF Depot",
                        "name": "Microsoft",
                        "isin": "US5949181045",
                        "ticker": "MSFT",
                        "asset_class": "Stock accumulating-like",
                        "quantity": "20.000000",
                        "latest_price": "350.00",
                        "current_value": "7000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-12-31",
                        "payout_date": "",
                    },
                    {
                        "account": "ETF Depot",
                        "name": "Allianz",
                        "isin": "DE0008404005",
                        "ticker": "ALV",
                        "asset_class": "Stock distributing",
                        "quantity": "20.000000",
                        "latest_price": "250.00",
                        "current_value": "5000.00",
                        "currency": "EUR",
                        "as_of_date": "2025-12-31",
                        "payout_date": "",
                    },
                ],
                {**snapshot_counts, "holdings": 7},
            ),
        )
        saved_review = SnapshotReview.objects.create(
            household=household,
            baseline_snapshot=baseline_snapshot,
            comparison_snapshot=comparison_snapshot,
            title="2025 annual review",
            review_date=date(2026, 1, 6),
            planned_summary="The family expected to grow the emergency buffer modestly and keep the ETF savings plan running.",
            actual_summary="Cash rose by 4,500 EUR, invested assets rose by 9,000 EUR, and mortgage debt fell by 20,000 EUR.",
            lessons_learned="The depot benefited from both monthly savings and market gains. Mortgage progress is easier to see when liabilities are reviewed together with investments.",
            next_actions="Keep the ETF savings plan active, review the mortgage refinance assumptions, and decide whether excess cash should move into Tagesgeld or the depot.",
        )
        SnapshotReviewAction.objects.create(
            review=saved_review,
            title="Review mortgage refinance assumptions",
            owner=people["alex"],
            due_date=date(2026, 3, 31),
            notes="Check whether the shorter fixed-interest tranche still matches the household risk tolerance.",
        )
        SnapshotReviewAction.objects.create(
            review=saved_review,
            title="Increase ETF savings plan after cash buffer review",
            owner=people["sam"],
            due_date=date(2026, 2, 15),
            status=SnapshotReviewAction.Status.DONE,
            notes="Demo completed action showing that review follow-ups can be closed.",
        )
        AssumptionReview.objects.create(
            household=household,
            key="household:inflation",
            label="Inflation",
            reviewed_by="Alex",
            note="Demo current review: 2% inflation still matches the household's conservative planning assumption.",
        )
        expired_assumption_review = AssumptionReview.objects.create(
            household=household,
            key="tax:capital-gains-tax",
            label="Capital gains tax",
            reviewed_by="Sam",
            note="Demo expired review: refresh this once real depot tax assumptions are entered.",
        )
        AssumptionReview.objects.filter(pk=expired_assumption_review.pk).update(
            reviewed_at=timezone.now() - timedelta(days=400)
        )

        # Second, deliberately separate demo household: a pure FIRE/depot-draw
        # story. fund_cash_goal_from_depot treats the cash goal as household
        # spending, so this household intentionally has no expense rules or
        # true expenses of its own — adding any would double-count against the
        # cash goal (see planner.quality's warning for that case).
        fire_household = Household.objects.create(
            name=FIRE_HOUSEHOLD_NAME,
            data_mode=Household.DataMode.DEMO,
            start_month=date(2026, 6, 1),
            planning_years=35,
            display_granularity=Household.DisplayGranularity.AUTO,
            annual_inflation_rate=Decimal("2.00"),
            capital_gains_tax_rate=Decimal("25.00"),
            fund_cash_goal_from_depot=True,
            currency="EUR",
        )
        Person.objects.create(
            household=fire_household,
            name="Jordan",
            role=Person.Role.ADULT,
            birth_date=date(1978, 3, 22),
            notes="Financially independent and retired early; the depot funds all living costs.",
        )
        fire_giro = AssetAccount.objects.create(
            household=fire_household,
            name="Girokonto",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("4000.00"),
            currency="EUR",
            source=AssetAccount.Source.MANUAL,
            institution="House bank",
            as_of_date=fire_household.start_month,
            notes="Small day-to-day buffer; the depot draw tops this up whenever spending outpaces it.",
        )
        fire_depot = AssetAccount.objects.create(
            household=fire_household,
            name="ETF Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("750000.00"),
            depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
            depot_annual_return_rate=Decimal("6.00"),
            depot_teilfreistellung_rate=Decimal("30.00"),
            depot_vorabpauschale_enabled=True,
            currency="EUR",
            source=AssetAccount.Source.MANUAL,
            institution="Broker",
            as_of_date=fire_household.start_month,
            notes=(
                "Demo FIRE portfolio. Valued via holdings using a classic German FIRE "
                "70/30 world/emerging-markets two-ETF split."
            ),
        )
        DepotHolding.objects.create(
            asset_account=fire_depot,
            name="iShares Core MSCI World UCITS ETF Acc",
            isin="IE00B4L5Y983",
            ticker="EUNL",
            asset_class="ETF accumulating",
            quantity=Decimal("5250.000000"),
            latest_price=Decimal("100.00"),
            currency="EUR",
            as_of_date=fire_household.start_month,
            notes="70% world-equity core position of the two-ETF FIRE portfolio.",
        )
        DepotHolding.objects.create(
            asset_account=fire_depot,
            name="iShares Core MSCI EM IMI UCITS ETF Acc",
            isin="IE00BKM4GZ66",
            ticker="IS3N",
            asset_class="ETF accumulating",
            quantity=Decimal("4500.000000"),
            latest_price=Decimal("50.00"),
            currency="EUR",
            as_of_date=fire_household.start_month,
            notes="30% emerging-markets satellite position of the two-ETF FIRE portfolio.",
        )
        fire_household.default_operating_account = fire_giro
        fire_household.save(update_fields=["default_operating_account", "updated_at"])
        CashGoal.objects.create(
            household=fire_household,
            name="FIRE living expenses",
            annual_amount=Decimal("28000.00"),
            indexed_to_inflation=True,
            start_year=2026,
            notes=(
                "Demo FIRE story: this cash goal is the household's only modeled spending. "
                "Portfolio funding treats it as an outflow and covers any shortfall from the "
                "depot, net of the capital-gains rate, so the Analytics and Year view pages "
                "show the drawdown mechanics without any expense rule double-counting it."
            ),
        )

        private_loan_count = household.private_loans.count()
        private_loan_label = "private loan" if private_loan_count == 1 else "private loans"
        self.stdout.write(
            self.style.SUCCESS(
                f"Created '{household.name}' with {household.people.count()} people, "
                f"{household.accounts.count()} accounts, {household.debts.count()} debts, "
                f"{household.income_investments.count()} income investment, "
                f"{private_loan_count} {private_loan_label}, "
                f"{household.properties.count()} properties, "
                f"{household.retirement_plans.count()} retirement plans, "
                f"{household.equity_grants.count()} equity grant, "
                f"{household.scenarios.count()} scenarios, {household.rules.count()} rules, "
                f"{household.transfer_rules.count()} transfer rules, "
                f"{household.family_gift_plans.count()} family gifts, "
                f"{household.real_estate_transfer_plans.count()} property transfer, "
                f"and {household.planned_investment_purchases.count()} planned investment purchase."
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Created '{fire_household.name}' with {fire_household.people.count()} person, "
                f"{fire_household.accounts.count()} accounts, and "
                f"{fire_household.cash_goals.count()} cash goal funded entirely from the depot."
            )
        )
