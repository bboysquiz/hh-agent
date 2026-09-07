from pathlib import Path


path = Path("hh_client.py")

text = path.read_text(encoding="utf-8")

old = '''                cards = await page.locator('a[data-qa="serp-item__title"]').all()
                if not cards:
                    break
                for card in cards:
                    href = await card.get_attribute("href")
                    title = (await card.inner_text()).strip()
                    if not href:
                        continue
                    job_id = _vacancy_id(href)
                    if not job_id:
                        continue
                    found_results += 1
'''

new = '''                vacancy_cards = await page.locator(
                    '[data-qa="vacancy-serp__vacancy"]'
                ).all()

                if not vacancy_cards:
                    break

                for vacancy_card in vacancy_cards:
                    # В обработку попадают только вакансии,
                    # на которые HH прямо сейчас разрешает откликнуться.
                    #
                    # Если пользователь уже откликался вручную или
                    # через другой инструмент, кнопки "Откликнуться"
                    # в карточке обычно больше нет.
                    response_button = vacancy_card.locator(
                        '[data-qa="vacancy-serp__vacancy_response"]'
                    ).first

                    try:
                        can_respond = (
                            await response_button.count() > 0
                            and await response_button.is_visible()
                        )
                    except Exception:
                        can_respond = False

                    if not can_respond:
                        logger.info(
                            "vacancy_skipped reason=no_response_button"
                        )
                        continue

                    card = vacancy_card.locator(
                        'a[data-qa="serp-item__title"]'
                    ).first

                    href = await card.get_attribute("href")
                    title = (await card.inner_text()).strip()

                    if not href:
                        continue

                    job_id = _vacancy_id(href)

                    if not job_id:
                        continue

                    found_results += 1
'''

if old not in text:
    raise SystemExit(
        "Expected block was not found. hh_client.py was not changed."
    )

backup = Path("hh_client.py.before-skip-applied.bak")
backup.write_text(text, encoding="utf-8")

text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")

print("OK: hh_client.py updated")
print(f"Backup: {backup}")