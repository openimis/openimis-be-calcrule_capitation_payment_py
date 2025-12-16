import decimal
import logging

from django.db.models import (
    Q,
    Sum,
    Count,
    F,
    Prefetch,
)
from django.db.models.functions import Coalesce

from django.contrib.contenttypes.models import ContentType

from calcrule_capitation_payment.config import (
    INTEGER_PARAMETERS,
    NONE_INTEGER_PARAMETERS
)
from claim.models import (
    ClaimItem,
    ClaimService
)
from claim.subqueries import elm_adjusted_exp
from claim_batch.models import (
    CapitationPayment
)
from claim_batch.services import (
    get_hospital_claim_filter,
    get_contribution_index_rate
)
from contribution_plan.utils import obtain_calcrule_params
from insuree.models import InsureePolicy
from invoice.models import Bill
from location.models import (
    Location,
    HealthFacility
)
from policy.models import Policy

logger = logging.getLogger(__name__)


def check_bill_not_exist(instance, health_facility, payment_plan, **kwargs):
    if instance.__class__.__name__ == "BatchRun":
        batch_run = instance
        content_type = ContentType.objects.get_for_model(batch_run.__class__)
        code = f"" \
            f"CP-{payment_plan.code}-{health_facility.code}" \
            f"-{batch_run.run_year}-{batch_run.run_month}"
        bills = Bill.objects.filter(
            subject_type=content_type,
            subject_id=batch_run.id,
            thirdparty_id=health_facility.id,
            code=code
        )
        if bills.exists() is False:
            return True


def claim_batch_valuation(payment_plan, work_data):
    """ update the service and item valuated amount """

    work_data["periodicity"] = payment_plan.periodicity
    items = work_data["items"]
    services = work_data["services"]
    start_date = work_data["start_date"]
    pp_params = work_data["pp_params"]
    # Sum up all item and service amount
    value = 0
    value_items = 0
    value_services = 0

    # if there is no configuration the relative index will be set to 100 %
    if start_date is not None:

        value_items = items.aggregate(sum=Sum(elm_adjusted_exp()))
        value_services = services.aggregate(sum=Sum(elm_adjusted_exp()))
        if 'sum' in value_items:
            value += value_items['sum'] if value_items['sum'] else 0
        if 'sum' in value_services:
            value += value_services['sum'] if value_services['sum'] else 0

        capitation_index, distribution = get_contribution_index_rate(value, pp_params, work_data)
        # update the item and services
        items.update(price_valuated=F('price_adjusted') * capitation_index * distribution)
        services.update(price_valuated=F('price_adjusted') * capitation_index * distribution)


def generate_capitation(payment_plan, start_date, end_date, allocated_contribution):
    pp_params = obtain_calcrule_params(payment_plan, INTEGER_PARAMETERS, NONE_INTEGER_PARAMETERS)
    product = payment_plan.benefit_plan
    population_matter = pp_params['weight_population'] > 0 or pp_params['weight_number_families'] > 0
    year = end_date.year
    month = end_date.month
    if pp_params['weight_insured_population'] > 0 or pp_params['weight_number_insured_families'] > 0 \
            or population_matter:
        # get location (district) linked to the product --> to be
        sum_pop, sum_families = 1, 1
        if population_matter:
            sum_pop, sum_families = get_product_sum_population(product)
        sum_insurees = 1
        # get the total number of insuree
        if pp_params['weight_insured_population'] > 0:
            sum_insurees = get_product_sum_insurees(product, start_date, end_date)
        # get the total number of insured family
        sum_insured_families = 1
        if pp_params['weight_number_insured_families'] > 0:
            sum_insured_families = get_product_sum_policies(product, start_date, end_date)
        # get the claim data
        sum_claim_adjusted_amount, sum_visits = 1, 1
        if pp_params['weight_number_visits'] > 0 or pp_params['weight_adjusted_amount'] > 0:
            sum_claim_adjusted_amount, sum_visits = get_product_sum_claim(product, start_date, end_date, pp_params)

        # select HF concerned with capitation within the product location (new HF will come from claims)
        health_facilities = get_product_hf_filter(pp_params, get_capitation_health_facilites(product, pp_params, start_date, end_date))
        health_facilities = health_facilities\
            .prefetch_related(Prefetch('location', queryset=Location.objects.filter(validity_to__isnull=True)))\
            .prefetch_related(Prefetch('location__parent', queryset=Location.objects.filter(validity_to__isnull=True)))

        # create n capitaiton report for each facilities
        for health_facility in health_facilities:
            # we might need to create the capitation report here with all the
            # common fields and run a class method generate_capitation_health_facility(product, hf)
            generate_capitation_health_facility(
                product, pp_params, health_facility, allocated_contribution,
                sum_insurees, sum_insured_families, sum_pop,
                sum_families, sum_claim_adjusted_amount, sum_visits,
                year, month, start_date, end_date
            )


def get_product_hf_filter(pp_params, queryset):
    # takes all HF if not level config is defined (ie. no filter added)
    if pp_params['hf_sublevel_1'] is not None or pp_params['hf_sublevel_2'] is not None \
            or pp_params['hf_sublevel_3'] is not None or pp_params['hf_sublevel_4'] is not None:
        # take the HF that match level and sublevel OR level if sublevel is not set in product
        queryset = queryset\
            .filter(
                (Q(level=pp_params['hf_level_1']) &
                    (Q(sub_level=pp_params['hf_sublevel_1']) | Q(sub_level__isnull=True))) |
                (Q(level=pp_params['hf_level_2']) &
                    (Q(sub_level=pp_params['hf_sublevel_2']) | Q(sub_level__isnull=True))) |
                (Q(level=pp_params['hf_level_3']) &
                    (Q(sub_level=pp_params['hf_sublevel_3']) | Q(sub_level__isnull=True))) |
                (Q(level=pp_params['hf_level_4']) &
                    (Q(sub_level=pp_params['hf_sublevel_4']) | Q(sub_level__isnull=True)))
            )
    return queryset


def generate_capitation_health_facility(
        product, pp_params, health_facility, allocated_contribution, sum_insurees, sum_insured_families,
        sum_pop, sum_families, sum_adjusted_amount, sum_visits, year, month, start_date, end_date
):
    population_matter = pp_params['weight_population'] > 0 or pp_params['weight_number_families'] > 0

    sum_hf_pop, sum_hf_families = 0, 0
    # get the sum of pop
    if population_matter:
        sum_hf_pop, sum_hf_families = get_hf_sum_population(health_facility)

    # get the sum of insuree
    sum_hf_insurees = 0
    if pp_params['weight_insured_population'] > 0:
        sum_hf_insurees = get_product_sum_insurees(product, start_date, end_date, health_facility)

    # get the sum of policy / insureed families
    sum_hf_insured_families = 0
    if pp_params['weight_number_insured_families'] > 0:
        sum_hf_insured_families = get_product_sum_policies(product, start_date, end_date, health_facility)

    sum_hf_claim_adjusted_amount, sum_hf_visits = 0, 0
    if pp_params['weight_number_visits'] > 0 or pp_params['weight_adjusted_amount'] > 0:
        sum_hf_claim_adjusted_amount, sum_hf_visits = get_product_sum_claim(product, start_date, end_date, pp_params, health_facility)

    # ammont available for all HF capitation
    allocated = (allocated_contribution * pp_params['share_contribution']) / 100

    # Allocated ammount for the Prodcut (common for all HF)
    alc_contri_population = (allocated * pp_params['weight_population']) / 100
    alc_contri_num_families = (allocated * pp_params['weight_number_families']) / 100
    alc_contri_ins_population = (allocated * pp_params['weight_insured_population']) / 100
    alc_contri_ins_families = (allocated * pp_params['weight_number_insured_families']) / 100
    alc_contri_visits = (allocated * pp_params['weight_number_visits']) / 100
    alc_contri_adjusted_amount = (allocated * pp_params['weight_adjusted_amount']) / 100

    # unit  (common for all HF)
    up_population = alc_contri_population / sum_pop if sum_pop > 0 else 0
    up_num_families = alc_contri_num_families / sum_families if sum_families > 0 else 0
    up_ins_population = alc_contri_ins_population / sum_insurees if sum_insurees > 0 else 0
    up_ins_families = alc_contri_ins_families / sum_insured_families if sum_insured_families > 0 else 0
    up_visits = alc_contri_visits / sum_visits if sum_visits > 0 else 0
    up_adjusted_amount = decimal.Decimal(alc_contri_adjusted_amount) / sum_adjusted_amount if sum_adjusted_amount > 0 else 0

    # amount for this HF
    total_population = sum_hf_pop * up_population
    total_families = sum_hf_families * up_num_families
    total_ins_population = sum_hf_insurees * up_ins_population
    total_ins_families = sum_hf_insured_families * up_ins_families
    total_claims = sum_hf_visits * up_visits
    total_adjusted = sum_hf_claim_adjusted_amount * up_adjusted_amount

    # overall total
    payment_cathment = total_population + total_families + total_ins_population + total_ins_families
    # Create the CapitationPayment so it can be retrieved from the invoice to generate the legacy reports
    if payment_cathment > 0:
        capitation = \
            CapitationPayment(
                year=year,
                month=month,
                product=product,
                health_facility=health_facility,
                region_code=health_facility.location.parent.code,
                region_name=health_facility.location.parent.code,
                district_code=health_facility.location.code,
                district_name=health_facility.location.code,
                health_facility_code=health_facility.code,
                health_facility_name=health_facility.name,
                hf_level=health_facility.level,
                hf_sublevel=health_facility.sub_level,
                total_population=total_population,
                total_families=total_families,
                total_insured_insuree=total_ins_population,
                total_insured_families=total_ins_families,
                total_claims=total_claims,
                total_adjusted=total_adjusted,
                alc_contri_population=alc_contri_population,
                alc_contri_num_families=alc_contri_num_families,
                alc_contri_ins_population=alc_contri_ins_population,
                alc_contri_ins_families=alc_contri_ins_families,
                payment_cathment=total_population + total_families + total_ins_population + total_ins_families,
                up_population=up_population,
                up_num_families=up_num_families,
                up_ins_population=up_ins_population,
                up_ins_families=up_ins_families,
                up_visits=up_visits,
                up_adjusted_amount=up_adjusted_amount
            )
        capitation.save()
    # TODO create bill with Capitation in the json_ext_details


# TODO  below might  be move to Product Module
def get_product_districts(product):
    districts = Location.objects.filter(validity_to__isnull=True)
    # if location null, it means all
    if product.location is None:
        districts = districts.all()
    elif product.location.type == 'D':
        # ideally we should just return the object but the caller will expect a queryset not an object
        districts = districts.filter(id=product.location.id)
    elif product.location.type == 'R':
        districts = districts.filter(parent_id=product.location.id)
    else:
        return None
    return districts


def get_product_villages(product):
    districts = get_product_districts(product)
    villages = None
    if districts is not None:
        villages = Location.objects.filter(validity_to__isnull=True)\
            .filter(parent__parent__in=districts)
    return villages


def get_capitation_health_facilites(product, pp_params, start_date, end_date):
    districts = get_product_districts(product)
    health_facilities_districts = HealthFacility.objects\
        .filter(validity_to__isnull=True)\
        .filter(location__in=districts)\
        .filter(get_hospital_level_filter(pp_params, prefix='claim__'))\
        .filter(get_hospital_claim_filter(product.ceiling_interpretation, pp_params['claim_type'], 'claim__'))
    # might need to add the items / services status
    health_facilities_off_districts = HealthFacility.objects\
        .filter(validity_to__isnull=True)\
        .filter(claim__validity_to__isnull=True)\
        .filter(claim__date_processed__lte=end_date)\
        .filter(claim__date_processed__gt=start_date)\
        .filter(get_hospital_level_filter(pp_params, prefix='claim__'))\
        .filter(get_hospital_claim_filter(product.ceiling_interpretation, pp_params['claim_type'], 'claim__'))\
        .filter((Q(claim__items__product=product) & Q(claim__items__validity_to__isnull=True))
                | (Q(claim__services__product=product) & Q(claim__services__validity_to__isnull=True)))

    if health_facilities_districts is not None:
        health_facilities = get_product_hf_filter(pp_params, health_facilities_districts | health_facilities_off_districts).distinct()
        return health_facilities
    else:
        return None


def get_hf_sum_population(health_facility):
    pop = Location.objects.filter(
        catchments__health_facility=health_facility,
        catchments__validity_to__isnull=True,
        *Location.filter_validity()).annotate(
            sum_pop=Sum((
                Coalesce(F('male_population'), 0)
                + Coalesce(F('female_population'), 0)
                + Coalesce(F('other_population'), 0)) * F('catchments__catchment') / 100))\
        .annotate(sum_families=Sum(Coalesce(F('families'), 0) * F('catchments__catchment') / 100))

    sum_pop, sum_families = 0, 0
    for p in pop:
        sum_pop += p.sum_pop or 0
        sum_families += p.sum_families or 0

    return sum_pop, sum_families


def get_product_sum_insurees(product, start_date, end_date, health_facility=None):
    villages = get_product_villages(product)
    if villages is not None:
        insurees = InsureePolicy.objects\
            .filter(validity_to__isnull=True)\
            .filter(insuree__family__location__in=villages)\
            .filter(policy__expiry_date__gte=start_date)\
            .filter(policy__effective_date__lte=start_date)\
            .filter(policy__product=product)
        # filter based on catchement if HF is defined
        if health_facility is None:
            insurees = insurees.annotate(sum=Count('id') / 100)
        else:
            insurees = insurees.filter(policy__family__location__catchments__health_facility=health_facility)\
                .filter(policy__family__location__catchments__validity_to__isnull=True)\
                .annotate(sum=Sum(F('policy__family__location__catchments__catchment')) * Count('id') / 100)
        sum_insuree = 0
        for insuree in insurees:
            sum_insuree += insuree.sum
        return sum_insuree
    else:
        return 0


def get_product_sum_policies(product, start_date, end_date, health_facility=None):
    villages = get_product_villages(product)
    if villages is not None:
        policies = Policy.objects\
            .filter(validity_to__isnull=True)\
            .filter(family__location__in=villages)\
            .filter(expiry_date__gte=start_date)\
            .filter(effective_date__lte=start_date)\
            .filter(product=product)
        # filter based on catchement if HF is defined
        if health_facility is None:
            policies = policies.annotate(sum=Count('id') / 100)
        else:
            policies = policies.filter(family__location__catchments__health_facility=health_facility)\
                .filter(family__location__catchments__validity_to__isnull=True)\
                .annotate(sum=Sum(F('family__location__catchments__catchment')) * Count('id') / 100)
        sum_policy = 0
        for policy in policies:
            sum_policy += policy.sum
        return sum_policy
    else:
        return 0


def get_product_sum_population(product):
    villages = get_product_villages(product)
    if villages is not None:
        pop = villages.annotate(sum_pop=Sum((F('male_population') + F('female_population') + F('other_population'))))\
            .annotate(sum_families=Sum((F('families'))))

        sum_pop, sum_families = 0, 0
        for p in pop:
            sum_pop += p.sum_pop if p.sum_pop else 0
            sum_families += p.sum_families if p.sum_families else 0

        return sum_pop, sum_families
    else:
        return 0, 0


def get_product_sum_claim(product, start_date, end_date, pp_params, health_facility=None):
    # make the items querysets
    items = ClaimItem.objects.filter(validity_to__isnull=True)\
        .filter(product=product)\
        .filter(claim__process_stamp__lte=end_date)\
        .filter(claim__process_stamp__gte=start_date)
    # make the services querysets
    services = ClaimService.objects.filter(validity_to__isnull=True)\
        .filter(product=product)\
        .filter(claim__process_stamp__lte=end_date)\
        .filter(claim__process_stamp__gte=start_date)
    # get the number of claims concened by the Items and services queryset
    if health_facility is not None:
        items = items.filter(claim__health_facility=health_facility)\
            .filter(get_hospital_level_filter(pp_params, prefix='claim__')) \
            .filter(get_hospital_claim_filter(product.ceiling_interpretation, pp_params['claim_type'], 'claim__'))
        services = services.filter(claim__health_facility=health_facility)\
            .filter(get_hospital_level_filter(pp_params, prefix='claim__')) \
            .filter(get_hospital_claim_filter(product.ceiling_interpretation, pp_params['claim_type'], 'claim__'))

    sum_visits = (
        items.values_list('claim_id', flat=True).distinct().union(services.values_list('claim_id', flat=True).distinct())
    ).count()

    sum_items = 0
    sum_services = 0
    value_items = items.aggregate(sum=Sum('price_valuated'))
    value_services = services.aggregate(sum=Sum('price_valuated'))

    if 'sum' in value_items:
        sum_items += value_items['sum'] if value_items['sum'] is not None else 0
    if 'sum' in value_services:
        sum_services += value_services['sum'] if value_services['sum'] is not None else 0

    return sum_items + sum_services, sum_visits


def get_hospital_level_filter(pp_params, prefix=''):
    qterm = Q()
    hf = '%shealth_facility' % prefix

    # if no filter all would be taken into account
    if pp_params['hf_level_1']:
        if pp_params['hf_sublevel_1']:
            qterm |= (Q(('%s__level' % hf, pp_params['hf_level_1'])) & Q(
                ('%s__sub_level' % hf, pp_params['hf_sublevel_1'])))
        else:
            qterm |= Q(('%s__level' % hf, pp_params['hf_level_1']))
    if pp_params['hf_level_2']:
        if pp_params['hf_sublevel_2']:
            qterm |= (Q(('%s__level' % hf, pp_params['hf_level_2'])) & Q(
                ('%s__sub_level' % hf, pp_params['hf_sublevel_2'])))
        else:
            qterm |= Q(('%s__level' % hf, pp_params['hf_level_2']))
    if pp_params['hf_level_3']:
        if pp_params['hf_sublevel_3']:
            qterm |= (Q(('%s__level' % hf, pp_params['hf_level_3'])) & Q(
                ('%s__sub_level' % hf, pp_params['hf_sublevel_3'])))
        else:
            qterm |= Q(('%s__level' % hf, pp_params['hf_level_3']))
    if pp_params['hf_level_4']:
        if pp_params['hf_sublevel_4']:
            qterm |= (Q(('%s__level' % hf, pp_params['hf_level_4'])) & Q(
                ('%s__sub_level' % hf, pp_params['hf_sublevel_4'])))
        else:
            qterm |= Q(('%s__level' % hf, pp_params['hf_level_4']))
    return qterm
