from vacancy_filter import title_rejection_reason


def test_rejects_golang_role_even_if_it_is_a_developer_title() -> None:
    assert title_rejection_reason(
        "Golang-разработчик (Middle+ / Senior)",
        (),
    ) == "golang"


def test_rejects_product_area_owner() -> None:
    assert title_rejection_reason(
        "Product Area Owner (Админский web и self-service)",
        (),
    ) == "product owner"


def test_rejects_backend_and_fullstack_vue_roles() -> None:
    assert title_rejection_reason(
        "Backend Developer (Node.js + Vue)",
        (),
    ) == "backend"
    assert title_rejection_reason(
        "Fullstack Developer (PHP + Vue)",
        (),
    ) == "fullstack"


def test_accepts_target_vue_frontend_titles() -> None:
    assert title_rejection_reason("Frontend-разработчик Vue 3", ()) is None
    assert title_rejection_reason("Senior Frontend Developer (Nuxt.js)", ()) is None
    assert title_rejection_reason("Vue.js / TypeScript Developer", ()) is None
    assert title_rejection_reason("Web-разработчик (Quasar, TS)", ()) is None


def test_rejects_ambiguous_non_frontend_title() -> None:
    assert title_rejection_reason("Software Engineer", ()) == "not_vue_frontend_role"
