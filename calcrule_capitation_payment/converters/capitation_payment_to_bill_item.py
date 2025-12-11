from django.contrib.contenttypes.models import ContentType


class CapitationPaymentToBillItemConverter(object):

    @classmethod
    def to_bill_line_item_obj(cls, capitation_payment, batch_run, payment_plan):
        bill_line_item = {}
        cls.build_line_fk(bill_line_item, capitation_payment)
        cls.build_dates(bill_line_item, batch_run)
        cls.build_code(bill_line_item, payment_plan)
        cls.build_description(bill_line_item, capitation_payment)
        cls.build_details(bill_line_item, capitation_payment)
        cls.build_quantity(bill_line_item)
        cls.build_unit_price(bill_line_item, capitation_payment)
        cls.build_discount(bill_line_item, capitation_payment)
        # cls.build_tax(bill_line_item)
        cls.build_amounts(bill_line_item)
        return bill_line_item

    @classmethod
    def build_line_fk(cls, bill_line_item, capitation_payment):
        bill_line_item["line_id"] = capitation_payment.id
        bill_line_item['line_type'] = ContentType.objects.get_for_model(capitation_payment)

    @classmethod
    def build_dates(cls, bill_line_item, batch_run):
        from core import datetimedelta
        bill_line_item["date_valid_from"] = batch_run.run_date
        bill_line_item["date_valid_to"] = batch_run.run_date + datetimedelta(days=30)

    @classmethod
    def build_code(cls, bill_line_item, payment_plan):
        bill_line_item["code"] = payment_plan.code

    @classmethod
    def build_description(cls, bill_line_item, capitation_payment):
        bill_line_item["description"] = "Capitation payment"

    @classmethod
    def build_details(cls, bill_line_item, capitation_payment):
        details = {
            "total_population": f'{capitation_payment.total_population}',
            "total_families": f'{capitation_payment.total_families}',
            "total_insured_insuree": f'{capitation_payment.total_insured_insuree}',
            "total_insured_families": f'{capitation_payment.total_insured_families}',
            "total_claims": f'{capitation_payment.total_claims}',
            "alc_contri_population": f'{capitation_payment.alc_contri_population}',
            "alc_contri_num_families": f'{capitation_payment.alc_contri_num_families}',
            "alc_contri_ins_population": f'{capitation_payment.alc_contri_ins_population}',
            "alc_contri_ins_families": f'{capitation_payment.alc_contri_ins_families}',
            "alc_contri_visits": f'{capitation_payment.alc_contri_visits}',
            "alc_contri_adjusted_amount": f'{capitation_payment.alc_contri_adjusted_amount}',
            "up_population": f'{capitation_payment.up_population}',
            "up_num_families": f'{capitation_payment.up_num_families}',
            "up_ins_population": f'{capitation_payment.up_ins_population}',
            "up_ins_families": f'{capitation_payment.up_ins_families}',
            "up_visits": f'{capitation_payment.up_visits}',
            "up_adjusted_amount": f'{capitation_payment.up_adjusted_amount}',
            "payment_cathment": f'{capitation_payment.payment_cathment}',
        }
        bill_line_item["details"] = details

    @classmethod
    def build_quantity(cls, bill_line_item):
        bill_line_item["quantity"] = 1

    @classmethod
    def build_unit_price(cls, bill_line_item, capitation_payment):
        bill_line_item["unit_price"] = capitation_payment.total_adjusted

    @classmethod
    def build_discount(cls, bill_line_item, capitation_payment):
        pass

    @classmethod
    def build_tax(cls, bill_line_item):
        bill_line_item["tax_rate"] = None
        bill_line_item["tax_analysis"] = None

    @classmethod
    def build_amounts(cls, bill_line_item):
        bill_line_item["amount_net"] = bill_line_item["quantity"] * bill_line_item["unit_price"]
        if "discount" in bill_line_item:
            bill_line_item["amount_net"] = bill_line_item["amount_net"] - bill_line_item["deduction"]
        bill_line_item["amount_total"] = bill_line_item["amount_net"]
