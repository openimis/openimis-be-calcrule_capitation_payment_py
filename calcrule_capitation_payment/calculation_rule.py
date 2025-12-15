from django.core.exceptions import ValidationError
from gettext import gettext as _

from core.abs_calculation_rule import AbsStrategy
from calcrule_capitation_payment.config import (
    CLASS_RULE_PARAM_VALIDATION,
    DESCRIPTION_CONTRIBUTION_VALUATION,
    FROM_TO,
    CONTEXTS,
    INTEGER_PARAMETERS,
    NONE_INTEGER_PARAMETERS,
)
from calcrule_capitation_payment.converters import (
    BatchRunToBillConverter,
    CapitationPaymentToBillItemConverter,
)
from calcrule_capitation_payment.legacy import get_capitation_region_and_district
from calcrule_capitation_payment.utils import (
    check_bill_not_exist,
    generate_capitation,
    get_hospital_level_filter,
    claim_batch_valuation,
)
from claim_batch.models import CapitationPayment
from claim_batch.services import (
    get_hospital_claim_filter,
    update_claim_valuated,
)
from core import datetime
from core.models import User
from contribution_plan.models import PaymentPlan
from contribution_plan.utils import obtain_calcrule_params
from invoice.services import BillService
from location.models import HealthFacility
from product.models import Product


class CapitationPaymentCalculationRule(AbsStrategy):
    version = 1
    uuid = "0a1b6d54-5681-4fa6-ac47-2a99c235eaa8"
    calculation_rule_name = "payment: capitation"
    description = DESCRIPTION_CONTRIBUTION_VALUATION
    impacted_class_parameter = CLASS_RULE_PARAM_VALIDATION
    date_valid_from = datetime.datetime(2000, 1, 1)
    date_valid_to = None
    status = "active"
    from_to = FROM_TO
    type = "account_payable"
    sub_type = "third_party_payment"

    @classmethod
    def active_for_object(
        cls, instance, context, type="account_payable", sub_type="third_party_payment"
    ):
        return (
            instance.__class__.__name__ == "PaymentPlan"
            and context in CONTEXTS
            and cls.check_calculation(instance)
        )

    @classmethod
    def check_calculation(cls, instance):
        class_name = instance.__class__.__name__
        match = False
        if class_name == "ABCMeta":
            match = str(cls.uuid) == str(instance.uuid)
        if class_name == "PaymentPlan":
            match = cls.uuid == str(instance.calculation)
        elif class_name == "BatchRun":
            # BatchRun → Product or Location if no prodcut
            match = cls.check_calculation(instance.location)
        elif class_name == "HealthFacility":
            #  HF → location
            match = cls.check_calculation(instance.location)
        elif class_name == "Location":
            #  location → ProductS (Product also related to Region if the location is a district)
            if instance.type in ["D", "R"]:
                products = Product.objects.filter(
                    location=instance, validity_to__isnull=True
                )
                for product in products:
                    if cls.check_calculation(product):
                        match = True
                        break
        elif class_name == "Product":
            # if product → paymentPlans
            payment_plans = PaymentPlan.objects.filter(
                benefit_plan=instance, is_deleted=False
            )
            for pp in payment_plans:
                if cls.check_calculation(pp):
                    match = True
                    break
        return match

    @classmethod
    def calculate(cls, instance, **kwargs):
        context = kwargs.get("context", None)
        if instance.__class__.__name__ == "PaymentPlan":
            if context == "BatchPayment":
                cls._process_batch_payment(instance, **kwargs)
                return "conversion finished 'capitation payment'"
            elif context == "BatchValuate":
                cls._process_batch_valuation(instance, **kwargs)
                return "valuation finished 'fee for service'"
            elif context == "IndividualPayment":
                pass
            elif context == "IndividualValuation":
                pass

    @classmethod
    def get_linked_class(cls, sender, class_name, **kwargs):
        list_class = super().get_linked_class(sender, class_name, **kwargs)
        # because we have calculation in PaymentPlan
        #  as uuid - we have to consider this case
        if class_name == "PaymentPlan":
            list_class.append("Calculation")
        return list_class

    @classmethod
    def convert(cls, instance, convert_to, **kwargs):
        context = kwargs.get("context", None)
        results = {}
        if context == "BatchPayment":
            hf = kwargs.get("health_facility", None)
            capitation_payments = kwargs.get("capitation_payments", None)
            payment_plan = kwargs.get("payment_plan", None)
            if check_bill_not_exist(instance, hf, payment_plan):
                convert_from = instance.__class__.__name__
                if convert_from == "BatchRun":
                    results = cls._convert_capitation_payment(
                        instance, hf, capitation_payments, payment_plan
                    )
                results["user"] = kwargs.get("user", None)
                BillService.bill_create(convert_results=results)
        return results

    @classmethod
    def _process_batch_valuation(cls, instance, **kwargs):
        work_data = kwargs.get("work_data", None)
        product = work_data["product"]
        pp_params = obtain_calcrule_params(
            instance, INTEGER_PARAMETERS, NONE_INTEGER_PARAMETERS
        )
        work_data["pp_params"] = pp_params
        # manage the in/out patient params
        work_data["claims"] = (
            work_data["claims"]
            .filter(get_hospital_level_filter(pp_params))
            .filter(
                get_hospital_claim_filter(
                    product.ceiling_interpretation, pp_params["claim_type"]
                )
            )
        )
        work_data["items"] = (
            work_data["items"]
            .filter(get_hospital_level_filter(pp_params, prefix="claim__"))
            .filter(
                get_hospital_claim_filter(
                    product.ceiling_interpretation, pp_params["claim_type"], "claim__"
                )
            )
        )
        work_data["services"] = (
            work_data["services"]
            .filter(get_hospital_level_filter(pp_params, prefix="claim__"))
            .filter(
                get_hospital_claim_filter(
                    product.ceiling_interpretation, pp_params["claim_type"], "claim__"
                )
            )
        )
        claim_batch_valuation(instance, work_data)
        update_claim_valuated(work_data["claims"], work_data["created_run"])

    @classmethod
    def _process_batch_payment(cls, instance, **kwargs):
        # get all valuated claims that should be evaluated
        #  with capitation that matches args (existing function develop in TZ scope)
        context = kwargs.get("context", None)
        audit_user_id, product_id, start_date, end_date, batch_run, work_data = (
            cls._get_batch_run_parameters(**kwargs)
        )

        # retrieving the allocated contribution from work_data
        if "allocated_contributions" in work_data:
            allocated_contribution = (
                work_data["allocated_contributions"] if not None else 0
            )
        else:
            allocated_contribution = 0

        # generating capitation report
        generate_capitation(instance, start_date, end_date, allocated_contribution)

        # do the conversion based on those params after generating capitation
        batch_run, capitation_payment, capitation_hf_list, user = (
            cls._process_capitation_results(instance.benefit_plan, **kwargs)
        )

        for chf in capitation_hf_list:
            capitation_payments = capitation_payment.filter(
                health_facility__id=chf["health_facility"]
            )
            hf = HealthFacility.objects.get(id=chf["health_facility"])
            # take batch run to convert capitation payments into bill per HF
            cls.run_convert(
                instance=batch_run,
                convert_to="Bill",
                user=user,
                health_facility=hf,
                capitation_payments=capitation_payments,
                payment_plan=instance,
                context=context,
            )

    @classmethod
    def _get_batch_run_parameters(cls, **kwargs):
        # TODO: test Batch run ID, update end_date, startdate
        audit_user_id = kwargs.get("audit_user_id", None)
        product_id = kwargs.get("product_id", None)
        start_date = kwargs.get("start_date", None)
        end_date = kwargs.get("end_date", None)
        work_data = kwargs.get("work_data", None)
        if work_data:
            batch_run = work_data["created_run"]
        else:
            batch_run = None
        return audit_user_id, product_id, start_date, end_date, batch_run, work_data

    @classmethod
    def _process_capitation_results(cls, product, **kwargs):
        audit_user_id, location_id, start_date, end_date, batch_run, work_data = (
            cls._get_batch_run_parameters(**kwargs)
        )
        # if this is trigerred by batch_run - take user data from audit_user_id
        user = User.objects.filter(i_user__id=audit_user_id).first()
        if user is None:
            raise ValidationError(_("Such User does not exist"))

        region_id, district_id, region_code, district_code = (
            get_capitation_region_and_district(batch_run.location_id)
        )

        capitation_payment = CapitationPayment.objects.filter(
            product=product,
            validity_to=None,
            region_code=region_code,
            year=end_date.year,
            month=end_date.month,
            total_adjusted__gt=0,
        )
        if district_code:
            capitation_payment = capitation_payment.filter(
                district_code=district_code,
            )

        capitation_hf_list = list(
            capitation_payment.values("health_facility").distinct()
        )

        return batch_run, capitation_payment, capitation_hf_list, user

    @classmethod
    def _convert_capitation_payment(
        cls, instance, health_facility, capitation_payments, payment_plan
    ):
        bill = BatchRunToBillConverter.to_bill_obj(
            batch_run=instance,
            health_facility=health_facility,
            payment_plan=payment_plan,
        )
        bill_line_items = []
        for cp in capitation_payments.all():
            bill_line_item = CapitationPaymentToBillItemConverter.to_bill_line_item_obj(
                capitation_payment=cp, batch_run=instance, payment_plan=payment_plan
            )
            bill_line_items.append(bill_line_item)
        return {
            "bill_data": bill,
            "bill_data_line": bill_line_items,
            "type_conversion": "batch run capitation payment - bill",
        }
