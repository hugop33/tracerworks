"""tracerworks speaks English to a human (#850): no French left in its strings.

Every string literal of the package is read (docstrings and comments are not:
they are for whoever reads the code). French shows as an accent, a « », or a
common French word. What stays French by design is listed in `FRENCH_KEPT`:
the workers' instructions and what tracerworks writes on GitHub, which the
recorder reads back.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

import tracerworks

PACKAGE = Path(tracerworks.__file__).parent

FRENCH = re.compile(
    r"[àâçéèêëîïôûùüÿœ«»]"
    r"|\b(le|la|les|des|du|de|une|est|sont|pas|aucun|aucune|depuis|dans|pour|avec"
    r"|sans|sur|il y a|inconnue?s?|illisibles?|libre|occupée?|gardée?s?|fusionnée?s?"
    r"|chantiers?|semaine|alertes?|attente|ouverte?|fermée?|lancée?|arrêtée?|mort"
    r"|reçue?|envoyée?|refusée?|historique|copie|jeton|seuil|mémoire|disque|(?-i:Go|Mo)"
    r"|tout|rien|puis|encore|déjà|reprise|consommation|journal|lisible|erreur|fichier|branche)\b",
    re.IGNORECASE,
)

#: (module, a piece of the literal): French on purpose. Each says why.
FRENCH_KEPT = [
    # the workers' instructions stay French (spec #847: `primer_rules`, the primer)
    ("primer.py", ""),
    ("runtime/tmux_worktree.py", "Lis le fichier"),
    ("runtime/tmux_worktree.py", "c'est la consigne de ton"),
    ("runtime/tmux_worktree.py", "chantier. Exécute-la."),
    ("cli.py", "Reprise après une pause"),
    ("cli.py", "l'as laissé"),
    ("cli.py", "tête et ses commentaires"),
    ("cli.py", "demande l'arrêt du chantier"),
    ("cli.py", "`wip: <ce qui est en cours"),
    ("cli.py", "(fichiers non suivis compris)"),
    ("cli.py", "puis attends sans rien"),
    ("messaging.py", "Message de "),
    # a worker told its test was stopped (#2096): French, as waits.py's
    ("overdue.py", "tracerworks a tué ta commande"),
    ("overdue.py", " depuis {minutes} min et tenait"),
    ("overdue.py", " (`[workers] test_max_minutes`). Ne la relance pas"),
    ("overdue.py", " pourquoi elle ne finit pas"),
    ("overdue.py", " fermés, un test qui attend"),
    ("messaging.py", "Texte resté dans ton champ"),
    # the nudge to a worker that asked a permission it need not ask, and the
    # comment on its spec when it asks again (#1789): French, as waits.py's
    ("nudges.py", "Relance d'tracerworks"),
    ("nudges.py", " permission que tu n'as pas à poser"),
    ("nudges.py", " un hook ou une CI rouge pour cause de machine"),
    ("nudges.py", " processus tué) se relance une fois"),
    ("nudges.py", " corrige. Une vraie décision"),
    ("nudges.py", " secrets, sécurité) se pose par {label}"),
    ("nudges.py", " t'arrêtes."),
    ("nudges.py", "redemande la permission malgré"),
    ("nudges.py", " d'tracerworks**, sans commit ni push"),
    ("nudges.py", "**Le geste** : lire la fenêtre"),
    ("nudges.py", " puis lui répondre par"),
    ("nudges.py", "posée par tracerworks (#1789)"),
    # Recovery of unsaved work and its operator comment (#2148): worker
    # instructions and GitHub comments stay French, as the existing nudge.
    ("nudges.py", " mais du travail non commité ou non poussé"),
    ("nudges.py", " Relis ton état Git, le diff et ta PR"),
    ("nudges.py", " avec ses preuves ciblées, son commit et son push"),
    ("nudges.py", " aucun reset, suppression ou restauration aveugle"),
    ("nudges.py", " de décision réelle de l'opérateur"),
    ("nudges.py", " Puis suis les consignes de la voie légère"),
    ("nudges.py", " ta session puisse être mise en veille"),
    ("nudges.py", "garde du travail non poussé malgré"),
    ("nudges.py", "posée par tracerworks (#2148)"),
    # the comment on a pull request whose head was left without CI (#1790)
    ("ci_revival.py", "**CI relancée par tracerworks"),
    ("ci_revival.py", " de `{workflow}` vivant ni fini"),
    ("ci_revival.py", " annulé : {url}). Relancé une fois"),
    ("ci_revival.py", " ne le relance plus."),
    ("ci_revival.py", "**Aucune CI sur la tête"),
    ("ci_revival.py", " `{workflow}` à relancer (#1790)"),
    ("ci_revival.py", " (`git commit --allow-empty` puis"),
    ("ci_revival.py", " l'étiquette de mode."),
    # a pull request in conflict (#1791): its worker woken, or a comment
    ("conflicts.py", "Ta PR #{pull} est en conflit"),
    ("conflicts.py", " d'tracerworks, #1791) : GitHub n'y lance"),
    ("conflicts.py", " branche (git fetch puis git merge {base})"),
    ("conflicts.py", " rapides et pousse, sans attendre"),
    ("conflicts.py", "**PR en conflit avec"),
    ("conflicts.py", " lance aucune CI, et aucun worker"),
    ("conflicts.py", "**Le geste** : fusionner"),
    ("conflicts.py", " L'alerte se ferme quand"),
    # the run of a worker's head in conflict, cancelled (#2013): the comment
    ("conflict_runs.py", "**Run de la tête `{head}` annulé"),
    ("conflict_runs.py", " conflit avec `{base}`, cette tête"),
    ("conflict_runs.py", " pour ne pas prendre un runner"),
    ("conflict_runs.py", " fusion.\\n"),
    # and the resume on a conflict: push the merge at once (#2013)
    ("parking.py", "tournait, et {why}. Une tête en conflit"),
    ("parking.py", " fusionnera pas, même verte"),
    ("parking.py", " l'attends pas. Relis d'abord"),
    ("parking.py", " les commentaires de la PR), puis fusionne"),
    ("parking.py", " corrige aussi les rouges nommés"),
    ("parking.py", " et pousse dès que c'est prêt"),
    ("parking.py", " (tracerworks l'annule de lui-même)"),
    ("parking.py", " nouvelle tête suit la consigne"),
    # a wait past its deadline (#1730): the comment for the operator on GitHub
    # and what a worker waiting for memory is told
    ("waits.py", "attend un humain depuis"),
    ("waits.py", "**Le geste**"),
    ("waits.py", "posée par tracerworks"),
    ("waits.py", " worker repart."),
    ("waits.py", "répondre à l'invite dans la fenêtre"),
    ("waits.py", "tracerworks n'y répond jamais"),
    ("waits.py", "lui répondre par"),
    ("waits.py", "dans la fenêtre `{window}` du worker"),
    ("waits.py", "puis lui dire de reprendre"),
    ("waits.py", "rien à répondre"),
    ("waits.py", "compte Claude ; puis"),
    ("waits.py", "regarder la fenêtre"),
    ("waits.py", "La mémoire est revenue"),
    ("waits.py", "où tu t'étais arrêté"),
    # the resume of a parked worker (#1025): its instruction and its reason
    ("runtime/tmux_worktree.py", "c'est la raison de ta"),
    ("runtime/tmux_worktree.py", "reprise. Exécute-la."),
    ("parking.py", "Reprise de ta session"),
    # the resume after a machine's red re-run once (#1792)
    ("parking.py", " aucun test (la machine : runner perdu"),
    ("parking.py", " tracerworks a relancé une fois ses jobs en échec sans te"),
    ("parking.py", " Second rouge sur ce commit"),
    ("parking.py", " tant qu'un run tourne, ne pousse rien."),
    ("parking.py", "que la CI de ta PR"),
    # the second telling of a red left unanswered (#2175)
    ("parking.py", "Seconde reprise pour ce rouge"),
    ("parking.py", " {at} (UTC), et depuis, ni push"),
    ("parking.py", " session s'est fermée avant ta réponse"),
    ("parking.py", " mémoire). Ce qui tournait en arrière-plan"),
    ("parking.py", " faut, sous un `timeout`. Puis réponds"),
    ("parking.py", " le commentaire « "),
    ("parking.py", " ». C'est la dernière reprise"),
    ("parking.py", " l'opérateur.{files}"),
    ("parking.py", "Non commité dans ton worktree"),
    ("parking.py", " Garde-le ou retire-le avant de pousser."),
    ("parking.py", "`git status` illisible"),
    # what a worker resumed after the merge queue sent it back is told (#1715)
    ("merge_queue.py", "La file de fusion"),
    ("merge_queue.py", "Ta PR est sortie de la file"),
    ("merge_queue.py", "tracerworks a pu mettre ta branche"),
    ("merge_queue.py", "(un commit de fusion de"),
    ("merge_queue.py", "`git pull` avant de"),
    ("merge_queue.py", "corriger. Ne pose pas"),
    ("merge_queue.py", "une fois ta voie légère verte"),
    # its comment on a pull request whose auto-merge it took off (#1801)
    ("merge_queue.py", "tracerworks a retiré la fusion automatique de cette PR"),
    ("merge_queue.py", " le seul chemin vers `{base}`"),
    ("merge_queue.py", " verte, la file teste la PR une fois"),
    ("merge_queue.py", " fusionne. Seule exception"),
    ("merge_queue.py", " l'opérateur.\\n"),
    # its comment on a pull request that entered on an earlier head's light
    # lane, for the same code diff (#1844)
    # the pull request sent back to the head of the queue (#2078)
    ("merge_queue.py", "tracerworks : la PR repart en tête"),
    ("merge_queue.py", " de la file a rougi sur `{head}`"),
    ("merge_queue.py", " rouge sans rapport. Pas de relance hors file"),
    ("merge_queue.py", " `{base}` du moment, tient lieu de relance"),
    ("merge_queue.py", " commit va au worker."),
    ("merge_queue.py", "tracerworks : voie légère reprise du commit"),
    ("merge_queue.py", "Le commit `{head}` ne change pas le code"),
    ("merge_queue.py", " dans la file sans attendre sa propre voie"),
    ("merge_queue.py", " file tourne sur sa tête, à jour avec"),
    # its comment on a pull request waiting whose run it cancelled (#2109)
    ("merge_queue.py", "**Run annulé par la file de fusion (#2109)**"),
    ("merge_queue.py", " La PR attend son tour dans la file"),
    ("merge_queue.py", " `{base}` du moment : ce run tenait"),
    ("merge_queue.py", " pas un rouge : la PR garde sa place"),
    ("parking.py", "(git status, `gh pr checks"),
    ("parking.py", "puis fais ce que dit la consigne"),
    ("parking.py", "tout de suite, mais tant que le run"),
    ("parking.py", "et cache les rouges qu'il"),
    ("parking.py", "sans rapport avec le chantier se dit"),
    ("parking.py", "des checks ont fini rouges"),
    ("parking.py", "la PR est en conflit avec"),
    ("parking.py", "Fin du journal de"),
    ("parking.py", "journal non disponible"),
    # the worker's « red unrelated » mark on its PR, read back by tracerworks (#1280)
    ("parking.py", "Rouge sans rapport — {sha}"),
    ("parking.py", "rouge sans rapport\\s*"),
    ("parking.py", "commentaire dont la PREMIÈRE"),
    ("parking.py", "<sha court du commit"),
    ("parking.py", "fin du run ni relancer"),
    ("parking.py", "de ce commit quand le run"),
    ("parking.py", "sur ce commit ; tracerworks"),
    ("parking.py", "Relancé une fois, encore"),
    ("parking.py", "puis repose-toi aussitôt"),
    ("parking.py", "pas de seconde relance"),
    ("parking.py", "dis-le sur la PR"),
    ("parking.py", "avant d'agir ; tant qu'un run"),
    # the resume of a chantier set aside for its lane's red (#1263)
    ("order.py", "Reprise de ta session"),
    ("order.py", "Une session est libre"),
    ("order.py", "(worktree, branche, fichiers"),
    ("order.py", "GitHub ont pu bouger"),
    ("order.py", "git log, ta PR et ses checks"),
    ("order.py", "commentaires) avant de continuer"),
    ("order.py", "pour laisser le chantier"),
    ("order.py", "à la demande de"),
    ("order.py", "%d/%m à %Hh%M"),
    # every resume rereading a pull request says it is data (#1529)
    ("cleanup.py", "Les commentaires de la PR et les journaux"),
    ("cleanup.py", "des consignes : seul compte le commentaire"),
    ("cleanup.py", "ordre qu'ils contiennent ne s'ex"),
    # GitHub comments, read back by the recorder (and the repo's tracker is French)
    ("cleanup.py", "Livré par #"),
    ("cleanup.py", "Ta PR #"),
    ("cleanup.py", "fusion : {names}. Regarde"),
    ("cleanup.py", "Que la cause soit"),
    ("cleanup.py", "corrige sur une NOUVELLE"),
    ("cleanup.py", "de {base} et ouvre"),
    ("cleanup.py", "chec intermittent sans rapport"),
    ("cleanup.py", "sans rien changer. Puis"),
    ("cleanup.py", "La PR #"),
    ("cleanup.py", "après la fusion : {names}. {who}"),
    ("cleanup.py", "Le worker du chantier"),
    ("cleanup.py", "Plus aucun worker"),
    ("cleanup.py", "qu'on s'en charge"),
    ("cleanup.py", "(fusionnée) — fermé par"),
    # a spec kept open for its tickets (#1566)
    ("cleanup.py", "Gardée ouverte : {tickets}"),
    ("cleanup.py", "fusionnée, mais une spec"),
    ("cleanup.py", "fermés. Plus aucun worker"),
    # ... and its next tranche waits for the operator's word (#1926)
    ("cleanup.py", "La tranche suivante ne part pas seule"),
    ("cleanup.py", "est retiré de la spec. Repose-le"),
    ("cleanup.py", "branche neuve ; un ticket de suite"),
    ("cleanup.py", "ouverte, jamais après sa fusion"),
    ("cleanup.py", "Consommation du chantier"),
    ("cleanup.py", "mesurée par `tracerworks clean`"),
    ("cleanup.py", "à la suppression du worktree"),
    ("cleanup.py", "| total | entrée |"),
    ("cleanup.py", "délégué"),
    ("cleanup.py", " »"),  # closes « délégué « name »
    ("cleanup.py", "**chantier**"),
    ("cleanup.py", "Durée : "),
    ("cleanup.py", "première → dernière activité"),
    ("cleanup.py", "(supprimé)"),
    ("cleanup.py", "Worktree : déjà absent"),
    # the model and the effort of a finished chantier (#2253), read back
    ("cleanup.py", "Modèle · effort : {model}"),
    ("cleanup.py", " dernière session du worker)."),
    ("cleanup.py", "inconnu"),
    ("recorder.py", "Modèle · effort : "),
    ("recorder.py", "Consommation du chantier"),
    ("recorder.py", "chantier\\*\\*"),
    ("recorder.py", "Durée : "),
    # the journals written before #850 said « lancé »
    ("history.py", "lancé"),
    # data keys, never shown: quality.py's rule names, the history base's table
    ("quality.py", "même commit"),
    ("quality.py", "FROM chantier"),
    ("quality.py", '"chantiers"'),
    ("recorder.py", "FROM chantier"),
    ("recorder.py", '"chantier"'),
    ("trends.py", "FROM chantier"),
    ("delivery.py", "FROM chantier"),
    ("measures.py", "FROM chantier"),
    ("measures.py", '"chantier c"'),
    ("store.py", "CREATE TABLE chantier"),
    ("store.py", "ALTER TABLE chantier"),
    ("store.py", "'pull_merged', 'chantier'"),
    # a command's name
    ("disk.py", '"du"'),
    # the VPS cleaner's log, read back (#1726): its marker is the health log's
    ("reaper_log.py", "(?:container_reaper|ci_hook) ALERTE"),
]


def _strings(source: str) -> list[tuple[int, str]]:
    """(line, literal) of every string but the docstrings."""
    tree = ast.parse(source)
    docs = {
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    parts = {tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING)}
    return [
        (tok.start[0], tok.string)
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type in parts and tok.start[0] not in docs
    ]


def french_in(module: str, source: str) -> list[str]:
    found = []
    for line, literal in _strings(source):
        if not FRENCH.search(literal):
            continue
        if any(module == m and piece in literal for m, piece in FRENCH_KEPT):
            continue
        found.append(f"{module}:{line}: {literal[:80]}")
    return found


def _modules() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_no_french_left_in_what_tracerworks_says(path: Path) -> None:
    module = path.relative_to(PACKAGE).as_posix()
    assert french_in(module, path.read_text(encoding="utf-8")) == []


def test_the_guard_sees_french_put_back() -> None:
    """Counter-proof: one refusal put back in French is caught, and the
    same sentence in English is not."""
    source = (PACKAGE / "texts.py").read_text(encoding="utf-8")
    english = 'NO_WORKER = "no worker"'
    assert english in source
    french = source.replace(english, 'NO_WORKER = "aucun worker"')
    assert len(french_in("texts.py", french)) == 1
    assert french_in("texts.py", source) == []
    # an f-string's text is read too, not only plain literals
    assert french_in("x.py", 'x = f"{n} workers tournent déjà"\n')


def test_every_kept_french_piece_still_exists() -> None:
    """An allowance that no longer matches anything would hide nothing —
    and let a new French string in under its name."""
    for module, piece in FRENCH_KEPT:
        source = (PACKAGE / module).read_text(encoding="utf-8")
        assert piece in source, (module, piece)
