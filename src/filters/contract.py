from typing import Literal, Optional


def has_monthly_contract(
    contract_monthly_raw: Optional[str],
) -> Literal["true", "false", "unknown"]:
    """
    Détermine si une annonce accepte un contrat mensuel (1 mois).

    Règles:
      - champ structuré "Contract monthly" != "-" et valeur présente -> "true"
      - champ structuré "Contract monthly" = "-"                      -> "false"
      - champ absent ou parsing impossible                            -> "unknown"

    Ne se base PAS sur la description textuelle.
    """
    if contract_monthly_raw is None:
        return "unknown"

    stripped = contract_monthly_raw.strip()

    if stripped == "" or stripped == "-":
        return "false"

    # Valeur présente (ex: "14,000 - 36,000 THB/month", "5,000 THB/month")
    return "true"


def has_short_term_contract(listing) -> bool:
    """
    Vrai si l'annonce propose un "Short-Term Rental Contract": un
    contrat 1, 3 ou 6 mois (shortContract=true côté source). Faux si
    l'annonce n'offre que de la location longue durée (ex: contrat
    1 an uniquement) ou seulement du journalier.
    """

    def present(v) -> bool:
        return v is not None and str(v).strip() not in ("", "-")

    return (
        getattr(listing, "has_monthly_contract", None) == "true"
        or present(getattr(listing, "contract_3_month_raw", None))
        or present(getattr(listing, "contract_6_month_raw", None))
    )
