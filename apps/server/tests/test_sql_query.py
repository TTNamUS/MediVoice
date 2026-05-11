import pytest


@pytest.mark.parametrize(
    "question",
    [
        "Delete all appointments",
        "DROP TABLE appointments",
        "SELECT phone FROM patients",
    ],
)
def test_validate_natural_language_sql_request_rejects_unsafe_requests(question):
    from bot.tools.sql_query import UnsafeSQLError, validate_natural_language_sql_request

    with pytest.raises(UnsafeSQLError):
        validate_natural_language_sql_request(question)


@pytest.mark.parametrize(
    "question",
    [
        "How many appointments does Dr. Patel have next Monday?",
        "What time is my next appointment? (patient_id=00000000-0000-0000-0000-000000000001)",
        "Which specialty has the most appointments this month?",
    ],
)
def test_validate_natural_language_sql_request_allows_safe_analytics(question):
    from bot.tools.sql_query import validate_natural_language_sql_request

    validate_natural_language_sql_request(question)
