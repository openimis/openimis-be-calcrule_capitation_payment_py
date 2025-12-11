from django.contrib.contenttypes.models import ContentType
from invoice.apps import InvoiceConfig
from invoice.models import Bill


class BatchRunToBillConverter(object):

    @classmethod
    def to_bill_obj(cls, batch_run, health_facility, payment_plan):
        bill = {}
        cls.build_subject(batch_run, bill)
        cls.build_thirdparty(health_facility, bill)
        cls.build_code(health_facility, payment_plan, batch_run, bill)
        cls.build_date_dates(batch_run, bill)
        # cls.build_tax_analysis(bill)
        cls.build_currency(bill)
        cls.build_status(bill)
        cls.build_terms(payment_plan, bill)
        return bill

    @classmethod
    def build_subject(cls, batch_run, bill):
        bill["subject_id"] = batch_run.id
        bill['subject_type'] = ContentType.objects.get_for_model(batch_run)

    @classmethod
    def build_thirdparty(cls, health_facility, bill):
        bill["thirdparty_id"] = health_facility.id
        bill['thirdparty_type'] = ContentType.objects.get_for_model(health_facility)

    @classmethod
    def build_code(cls, health_facility, payment_plan, batch_run, bill):
        bill["code"] = f"" \
            f"CP-{payment_plan.code}-{health_facility.code}" \
            f"-{batch_run.run_year}-{batch_run.run_month}"

    @classmethod
    def build_date_dates(cls, batch_run, bill):
        from core import datetimedelta
        bill["date_due"] = batch_run.run_date + datetimedelta(days=30)
        bill["date_bill"] = batch_run.run_date
        bill["date_valid_from"] = batch_run.run_date
        # TODO - explain/clarify meaning of 'validity to' of this field
        # bill["date_valid_to"] = batch_run.expiry_date

    @classmethod
    def build_tax_analysis(cls, bill):
        bill["tax_analysis"] = None

    @classmethod
    def build_currency(cls, bill):
        bill["currency_tp_code"] = InvoiceConfig.default_currency_code
        bill["currency_code"] = InvoiceConfig.default_currency_code

    @classmethod
    def build_status(cls, bill):
        bill["status"] = Bill.Status.VALIDATED.value

    @classmethod
    def build_terms(cls, payment_plan, bill):
        bill["terms"] = payment_plan.benefit_plan.name

    @classmethod
    def build_amounts(cls, line_item, bill_update):
        bill_update["amount_net"] = line_item["amount_net"]
        bill_update["amount_total"] = line_item["amount_total"]
        bill_update["amount_discount"] = 0 if line_item["discount"] else line_item["discount"]
