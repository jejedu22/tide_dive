"""
Gestion de la base SQLite de l'application.

La base stocke, par port :
  - les infos du port (nom, coordonnées, fuseau horaire)
  - une série temporelle de hauteurs d'eau (pas de 10 min par défaut)
  - les extrema de marée (pleines mers / basses mers) avec coefficient estimé
  - les horaires de lever/coucher de soleil civils et le crépuscule nautique

Cette base est peuplée année par année par le script `precompute.py`, puis
interrogée en lecture seule par l'application web. Aucun calcul de marée
n'est refait à la volée lors de la navigation.

Organisation (ce module est une façade : tout y est réexporté, le reste du code écrit `db.fonction(...)`) :
  db_core.py        connexion (get_conn) et réglages de l'application
  db_schema.py      schéma SQL, migrations historiques, init_db
  db_tides.py       ports, recalage, marées, soleil, vacances scolaires
  db_structures.py  structures
  db_users.py       comptes, sessions, jetons, préférences
  db_memberships.py appartenances à plusieurs structures, structure active, invitations
  db_jobs.py        file de tâches, worker
  db_selections.py  types de créneaux, créneaux choisis, inscriptions
  db_unavailabilities.py plages d'indisponibilité des structures
  db_water.py       hauteurs d'eau des ports, créneaux de hauteur d'eau
  db_requests.py    demandes de création de structure
  db_newsletters.py Mailjet, newsletters, groupes d'envoi
Les évolutions nouvelles du schéma : migrations.py.
"""

from __future__ import annotations

from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "plongee.db"

# Réexportations : après DB_PATH, que db_core relit à chaque appel (get_conn)
from .db_core import (  # noqa: F401
    get_conn,
    get_setting,
    set_setting,
)
from .db_schema import (  # noqa: F401
    SLOT_SELECTIONS_COLUMNS,
    TIDE_HEIGHTS_COLUMNS,
    TIDE_EXTREMA_COLUMNS,
    SCHEMA,
    INDEXES_AFTER_MIGRATION,
    init_db,
    _columns,
    DEFAULT_STRUCTURE_NAME,
    _migrate_structures,
    _migrate_custom_selections,
    _migrate,
)
from .db_tides import (  # noqa: F401
    _SQL_INSERT_HEIGHTS,
    _SQL_INSERT_EXTREMA,
    _SQL_INSERT_SUN,
    SOURCES,
    DEFAULT_SOURCES,
    _LAST_RESORT,
    tide_sources,
    _iso,
    _merge,
    _subtract,
    _coverage,
    _set_coverage,
    cover,
    uncover,
    _plan,
    _compose,
    upsert_port,
    list_ports,
    create_port,
    _PORT_FIELDS,
    update_port,
    delete_port,
    years_by_port,
    models_by_port_year,
    _CALIBRATION_FIELDS,
    save_calibration,
    get_calibration,
    calibrations_by_port,
    delete_calibration,
    get_port,
    _year_bounds,
    replace_year,
    REBIND_TOLERANCE,
    structure_sources,
    _rebind_selections,
    rebind_moves,
    update_window,
    rebind_water_selections,
    rebind_water_for_structure,
    replace_range,
    get_heights_range,
    save_short_term_window,
    short_term_windows,
    clear_port_data,
    years_available,
    insert_heights,
    insert_extrema,
    insert_sun_times,
    get_extrema_range,
    get_extremum,
    get_structure_sources,
    get_sun_times_range,
    replace_school_holidays,
    get_school_holidays_range,
    school_holidays_coverage,
)
from .db_structures import (  # noqa: F401
    _STRUCTURE_SELECT,
    list_structures,
    get_structure,
    create_structure,
    rename_structure,
    LOCK_COLUMNS,
    SETTINGS_COLUMNS,
    update_structure_settings,
    DEFAULT_RDV_OFFSET_MINUTES,
    get_rdv_offset,
    get_default_max_registrations,
    get_lock_days,
    delete_structure,
)
from .db_users import (  # noqa: F401
    _user_query,
    _ORDER_USERS,
    UNUSABLE_PASSWORD,
    list_users,
    get_user,
    get_user_credentials,
    get_user_credentials_by_id,
    username_exists,
    existing_logins,
    _PROFILE_FIELDS,
    _insert_user,
    create_user,
    create_users_bulk,
    update_user,
    set_user_profiles,
    delete_user,
    count_admins,
    create_session,
    get_session_user,
    PREVIEW_ROLES,
    _STRUCTURE_FIELDS,
    _previewed,
    set_session_preview,
    delete_session,
    delete_other_sessions,
    create_user_token,
    get_token,
    last_token_at,
    delete_user_tokens,
    _CLEAR_TEMP,
    set_temp_password,
    clear_temp_password,
    get_preferences,
    save_preferences,
    delete_preferences,
)
from .db_memberships import (  # noqa: F401
    _sync_default,
    list_memberships,
    get_membership,
    count_memberships,
    _add_membership,
    add_membership,
    set_membership_role,
    remove_membership,
    set_active_structure,
    create_invitation,
    list_invitations,
    delete_invitation,
    invitations_for_user,
    answer_invitation,
)
from .db_jobs import (  # noqa: F401
    JOB_LOG_MAX,
    JOBS_KEPT,
    _prune_jobs,
    _JOB_COLUMNS,
    enqueue_job,
    list_jobs,
    get_job,
    request_job_cancel,
    claim_next_job,
    append_job_log,
    job_cancel_requested,
    finish_job,
    fail_orphan_jobs,
    worker_heartbeat,
    get_worker_status,
)
from .db_selections import (  # noqa: F401
    list_slot_types,
    get_slot_type,
    create_slot_type,
    _SLOT_TYPE_FIELDS,
    update_slot_type,
    reorder_slot_types,
    delete_slot_type,
    _SELECTION_SQL,
    list_selections,
    get_selection,
    update_selection_tide,
    update_selection_rdvs,
    create_selection,
    create_custom_selection,
    update_custom_selection,
    update_selection_note,
    update_selection_type,
    delete_selection,
    list_registrations,
    add_registration,
    delete_registration,
    update_selection_capacity,
)
from .db_unavailabilities import (  # noqa: F401
    _UNAV_START,
    _UNAV_END,
    _UNAV_SEL_START,
    _UNAV_SEL_END,
    _UNAV_SQL,
    list_unavailabilities,
    get_unavailability,
    create_unavailability,
    update_unavailability,
    delete_unavailability,
    selections_in_unavailability,
)
from .db_water import (  # noqa: F401
    list_thresholds,
    get_threshold,
    create_threshold,
    update_threshold,
    delete_threshold,
    create_water_selection,
    water_selections_between,
)
from .db_requests import (  # noqa: F401
    create_structure_request,
    count_structure_requests_since,
    list_structure_requests,
    get_structure_request,
    set_structure_request_status,
    delete_structure_request,
    purge_structure_requests,
    super_admin_emails,
)
from .db_newsletters import (  # noqa: F401
    get_mailjet,
    save_mailjet,
    save_mailjet_check,
    delete_mailjet,
    set_mailjet_events,
    structure_by_events_token,
    _NL_STATS,
    _NL_SELECT,
    _NL_FIELDS,
    create_newsletter,
    update_newsletter,
    get_newsletter,
    list_newsletters,
    delete_newsletter,
    due_newsletters,
    audience_members,
    unsubscribed_emails,
    add_unsubscribe,
    remove_unsubscribe,
    list_unsubscribes,
    insert_recipients,
    queued_recipients,
    mark_recipients,
    list_recipients,
    clicked_links,
    recipient_for_event,
    recipient_by_token,
    _EVENT_COLUMNS,
    record_event,
    list_mailing_groups,
    get_mailing_group,
    mailing_group_member_ids,
    save_mailing_group,
    delete_mailing_group,
    structure_members,
)
