from collections import Counter

from main import (
    LLM_REJECTION_REASON_PREFIX,
    SearchRunStats,
    VacancyProcessResult,
    format_reason_summary,
    humanize_circuit_reason,
    humanize_error_reason,
    humanize_rejection_reason,
)


def test_llm_rejection_is_human_readable() -> None:
    assert (
        humanize_rejection_reason("llm_rejected")
        == "отклонено ИИ-анализом"
    )


def test_detailed_llm_rejection_is_preserved() -> None:
    reason = (
        "Основные задачи относятся к backend-разработке. "
        "Vue упомянут только как дополнительная технология."
    )

    assert humanize_rejection_reason(f"llm_reason:{reason}") == (
        f"ИИ-анализ: {reason}"
    )


def test_stats_store_detailed_llm_rejection_instead_of_generic_label() -> None:
    reason = (
        "Основной стек вакансии — Angular. "
        "Vue в рабочих задачах не используется."
    )
    stats = SearchRunStats()

    stats.record(
        VacancyProcessResult(
            outcome="rejected_by_llm",
            reason=reason,
        )
    )

    assert stats.rejected_by_llm == 1
    assert stats.rejection_reasons == {
        f"{LLM_REJECTION_REASON_PREFIX}{reason}": 1
    }


def test_lead_filter_is_human_readable() -> None:
    assert (
        humanize_rejection_reason("lead")
        == "вакансии уровня Lead"
    )


def test_senior_filter_is_human_readable() -> None:
    assert (
        humanize_rejection_reason("senior")
        == "вакансии уровня Senior"
    )


def test_fullstack_filter_is_human_readable() -> None:
    assert (
        humanize_rejection_reason("Fullstack")
        == "Fullstack-вакансии"
    )


def test_unknown_title_filter_is_still_readable() -> None:
    assert (
        humanize_rejection_reason("Product Owner")
        == "фильтр по названию «Product Owner»"
    )


def test_network_error_is_human_readable() -> None:
    assert (
        humanize_error_reason("network_error")
        == "ошибка сети"
    )


def test_llm_timeout_is_human_readable() -> None:
    assert (
        humanize_error_reason(
            "analysis_failed:timeout"
        )
        == "тайм-аут ИИ-анализа"
    )


def test_unknown_technical_error_does_not_leak_code() -> None:
    assert (
        humanize_error_reason("SomeInternalException")
        == "техническая ошибка"
    )


def test_circuit_reason_is_human_readable() -> None:
    assert (
        humanize_circuit_reason(
            "technical_failure_ratio"
        )
        == "слишком много технических ошибок"
    )


def test_reason_summary_matches_telegram_example() -> None:
    rejection_reasons = Counter(
        {
            "llm_rejected": 2,
            "lead": 1,
        }
    )

    result = format_reason_summary(
        rejection_reasons,
        Counter(),
    )

    assert result == (
        "• отклонено ИИ-анализом — 2\n"
        "• вакансии уровня Lead — 1"
    )


def test_reason_summary_handles_fullstack() -> None:
    rejection_reasons = Counter(
        {
            "llm_rejected": 2,
            "Fullstack": 2,
        }
    )

    result = format_reason_summary(
        rejection_reasons,
        Counter(),
    )

    assert result == (
        "• отклонено ИИ-анализом — 2\n"
        "• Fullstack-вакансии — 2"
    )


def test_reason_summary_includes_errors() -> None:
    rejection_reasons = Counter(
        {
            "senior": 1,
        }
    )

    error_reasons = Counter(
        {
            "network_error": 2,
        }
    )

    result = format_reason_summary(
        rejection_reasons,
        error_reasons,
    )

    assert result == (
        "• ошибка сети — 2\n"
        "• вакансии уровня Senior — 1"
    )


def test_empty_reasons_return_empty_string() -> None:
    assert (
        format_reason_summary(
            Counter(),
            Counter(),
        )
        == ""
    )


def test_only_three_most_common_reasons_are_returned() -> None:
    rejection_reasons = Counter(
        {
            "llm_rejected": 10,
            "senior": 5,
            "lead": 4,
            "Fullstack": 3,
        }
    )

    result = format_reason_summary(
        rejection_reasons,
        Counter(),
    )

    assert result == (
        "• отклонено ИИ-анализом — 10\n"
        "• вакансии уровня Senior — 5\n"
        "• вакансии уровня Lead — 4"
    )

    assert "Fullstack" not in result
