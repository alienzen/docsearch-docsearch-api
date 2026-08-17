# docsearch-api

API REST de recherche pour **DocSearch** — FastAPI, filtrage par ACL,
aperçu de documents. Fait partie de l'écosystème DocSearch :

| Dépôt | Rôle |
|---|---|
| [docsearch-ingestion](../docsearch-ingestion) | Extraction, ACL, indexation |
| **docsearch-api** (ce dépôt) | API de recherche |
| [docsearch-ui-vue](../docsearch-ui-vue) | Interface web (Vue 3 + DSFR) |
| [docsearch-infra](../docsearch-infra) | Orchestration podman + systemd (Quadlet) |
| [docsearch-docs](../docsearch-docs) | Documents commerciaux |
| `docsearch-dataset-generator` | Génération de jeux de test (cloné à la demande) |

Ce dépôt ne dépend d'aucun autre : il lit uniquement un index Elasticsearch
déjà peuplé (par `docsearch-ingestion`). Aucun couplage de code.

## Endpoints

| Méthode | Route | Description |
|---|---|---|
| GET  | `/health` | Santé du service + version ES |
| POST | `/search` | Recherche full-text filtrée par ACL |
| GET  | `/document/{id}` | Détail d'un document (vérifie l'ACL **et** que la source est cherchable par l'appelant) |
| GET  | `/document/{id}/similar` | Documents similaires (More Like This) |
| GET  | `/api/preview/{id}` | Aperçu PDF, texte ou image océrisée (conversion LibreOffice si besoin) — mêmes contrôles que `/document/{id}` |
| GET  | `/metrics` | Statistiques d'indexation |
| GET  | `/admin/retention` | Ce que la purge quotidienne des journaux emporterait, sans rien supprimer |
| GET  | `/admin/duplicates` | Documents indexés en plusieurs exemplaires, et place occupée |
| GET/POST/DELETE | `/admin/synonyms[/{id}]` | Thésaurus métier — effet immédiat, sans réindexation |
| GET/POST | `/admin/pinned` | Résultats épinglés sur une requête (mise en avant, jamais autorisation) |
| POST | `/admin/synonyms/test` | Ce que le moteur comprend d'une requête, synonymes appliqués |
| GET/POST/DELETE | `/saved-searches` | Recherches enregistrées par utilisateur |
| PATCH | `/saved-searches/{id}/alert` | Active/désactive l'alerte d'une recherche enregistrée (fréquence quotidienne/hebdomadaire) |
| GET  | `/alerts` | Notifications in-app de l'utilisateur (nouveaux résultats détectés par `alert_worker.py`) |
| POST | `/alerts/{id}/seen`, `/alerts/mark-all-seen` | Marque une/toutes les notifications comme lues |
| GET  | `/me/searches` | Historique de recherche de l'utilisateur courant (le sien, et rien d'autre) |
| DELETE | `/me/searches` | Efface cet historique : ses recherches passées sont **anonymisées** dans le journal |
| GET  | `/me/recent-documents` | Derniers documents qu'il a ouverts, relus à travers l'ACL |
| DELETE | `/me/recent-documents` | Efface cette liste : le détail des clics est **supprimé** du journal, seul leur nombre reste |
| POST | `/collections/{id}/share`, `.../duplicate` | Partage d'une collection avec ses groupes, et copie |
| GET  | `/search/suggest` | Suggestions de saisie : ses recherches passées, puis auteurs et mots-clés visibles |
| GET  | `/searchable-sources` | Sources cherchables, pour la présélection avant recherche |
| GET/POST/DELETE | `/collections` | Collections de documents personnelles ("📋 Mes collections") |
| POST | `/collections/{id}/rename`, `/collections/{id}/documents`, `/collections/{id}/documents/{doc_id}` | Gestion du contenu d'une collection |
| GET  | `/ui-config` | Bascules d'interface publique (lien Assistant IA, pied de page, export...) |
| GET  | `/is-admin` | Indique si l'utilisateur courant a accès au panneau d'administration |
| GET  | `/engagement-config` | Bascules de mesure de satisfaction (pouce, NPS, suggestions) |
| POST | `/feedback`, `/click`, `/nps`, `/suggestions` | Signaux de mesure de satisfaction (voir "Mesure de satisfaction" dans l'admin) |

⚠️ **Il n'y a pas de route `/ask`.** Ce tableau en a listé une, décrite comme
un « assistant conversationnel (RAG) » — elle n'a jamais existé dans le code.
La page `chat.html` de l'interface est une **maquette** qui joue des réponses
écrites à l'avance (`docsearch-ui-vue/src/pages/chat/cannedResponses.ts`) :
aucun document indexé n'est interrogé, aucun modèle de langage n'est branché.
Le lien « Assistant IA » de l'en-tête est masqué par défaut depuis le
2026-08-15 (`chat_enabled` vaut `false`) : l'afficher depuis le panneau
d'administration expose cette maquette, qui peut passer pour une
fonctionnalité réelle. Voir `docsearch-infra/FEATURES.md` et
`docsearch-infra/PLAN-EVOLUTIONS.md` (§5, en attente d'arbitrage).

**Recherche de phrase** : entourer la requête de guillemets
(`"terme exact"`) force une correspondance de phrase (ordre et adjacence
des mots respectés, sans tolérance aux fautes de frappe), au lieu de la
recherche par défaut, qui tolère les variantes et les fautes de frappe.

**Tolérance aux fautes** : la recherche ordinaire rend un `bool` à deux
branches en OU — les champs ordinaires (racinisation + thésaurus) sans
`fuzziness`, et les sous-champs `.exact` avec `fuzziness: "AUTO:5,99"`.
Ce n'est pas un raffinement : Lucene abandonne la `fuzziness` sur toute
position élargie par le thésaurus, donc une clause unique perdait la
tolérance aux fautes sur les termes mêmes que l'administrateur venait
d'enrichir — avec MOINS de résultats qu'avant l'ajout de la règle. Le
plafond `AUTO:5,99` (aucune correction sous cinq caractères, une seule
au-delà) remplace le `AUTO` d'Elasticsearch, trop lâche sur un corpus
français où `délégation`/`dérogation` et `convention`/`conviction` sont
à deux corrections l'un de l'autre. Voir `build_text_clause` dans
`app/search_query.py`.

**Recherche exacte** : `exact: true` (booléen, `false` par défaut)
interroge les sous-champs `.exact` au lieu des champs ordinaires. Ces
sous-champs sont analysés sans racinisation, sans mots vides et sans
synonymes, mais avec `lowercase` + `asciifolding` : `Congrès`,
`congres` et `CONGRES` sont une seule et même requête, tandis que
`délégations` cesse de répondre à `délégation`. La tolérance aux fautes
est désactivée — elle rattraperait précisément les écarts que ce mode
sert à conserver.

⚠️ **Les deux dimensions sont indépendantes et se combinent.** Les
guillemets portent sur l'enchaînement des mots (« dans cet ordre »),
`exact` sur la façon dont chaque mot est comparé (« tel qu'écrit ») :
les quatre combinaisons ont un sens. Côté interface, `exact` est porté
par une case à cocher près de la barre de recherche et par l'opérateur
`exact:` de la syntaxe avancée, qui produisent le même critère.

⚠️ **Les sous-champs `.exact` n'existent que sur les index migrés.** Un
`multi_match` visant un champ absent du mapping ne lève aucune erreur :
il ne matche rien. Une source qui n'a pas reçu
`./manage.sh migrer-exact --apply` est donc **silencieusement muette**
en recherche exacte, sans le moindre signal dans les journaux. La
migration couvre les trois familles d'index (fichiers, SQL, web), pose
l'analyseur (fermeture/réouverture de l'index, quelques secondes) et
réécrit sur place les documents déjà indexés (`_update_by_query`, sans
Tika ni relecture disque, lancé en tâche de fond côté Elasticsearch).

**Recherche restreinte à un champ** : `search_in` (`"all"` par défaut,
`"title"`, `"author"` ou `"filepath"`) limite la recherche en texte
libre à un seul champ plutôt que tous — `"all"` interroge `content`,
`title`, `filename` et `author.text`. `author` et `filepath`
interrogent leurs sous-champs analysés respectifs (`author.text`,
`filepath.text`) plutôt que les champs racine, qui sont en `keyword`
(non tokenisés — nécessaires pour le filtre exact des facettes et
`purge_path`/`is_path_allowed`, mais incompatibles avec une recherche
partielle en texte libre). ⚠️ Ces sous-champs ne sont peuplés que pour
les documents indexés après l'ajout de ce mapping — une réindexation
est nécessaire pour que les documents déjà présents deviennent
cherchables par ce biais.

## Temps de recherche

`POST /search` renvoie, à côté de `total`/`results`/`facets` :

```json
"timing": { "took_ms": 41, "duration_ms": 128.4 }
```

- `took_ms` : temps passé DANS Elasticsearch, rapporté par ES lui-même.
- `duration_ms` : temps total du endpoint (résolution ACL, construction
  de la requête, appel ES), hors écriture du journal de recherche —
  celle-ci n'est pas du temps de recherche, et l'y inclure ferait passer
  une panne du journal pour une lenteur du moteur.

Aucune des deux ne compte l'aller-retour réseau : ce que l'utilisateur
attend est toujours un peu plus. Leur **écart** est l'information utile —
3000 ms dont 2900 dans le moteur et 3000 ms dont 200 dans le moteur
n'appellent pas la même correction.

Les deux valeurs sont enregistrées dans chaque document de l'index
`search_logs` (champs `took_ms` et `duration_ms`, ajoutés au mapping des
index existants par `put_mapping` au premier démarrage). Elles alimentent
les indicateurs de `stats.html` (moyenne, médiane, 95ᵉ centile, nombre de
recherches lentes) et les deux dernières colonnes de l'export XLSX de
l'historique. Les recherches antérieures à la mise en place de la mesure
n'ont pas ces champs : `/admin/search-logs/summary` remonte donc
`timing.measured`, le nombre de recherches réellement mesurées, que la
page affiche à côté des moyennes.

**Recherches véritables et tours de page.** Chaque clic sur « Suivant »
relance `POST /search` et écrit une ligne de journal de plus, identique à
la précédente. Le champ `page` (1 pour une recherche, 2+ pour un tour de
page), dérivé de `from`/`size`, permet de les distinguer ; `exclude_pagination=true`
les écarte de `GET /admin/search-logs` et de son export. Le champ `exact`
est enregistré au même endroit et pour la même raison : deux lignes de
même requête et de comptes différents s'expliquent souvent par lui seul.

⚠️ **Les deux champs sont ABSENTS des lignes antérieures**, où ils valent
« inconnu » et non « page 1 » / « non exacte ». Le filtre est donc un
`must_not page > 1` et non un `term page = 1` : les lignes anciennes
restent affichées, faute de savoir ce qu'elles étaient.

**Les agrégats appliquent le même filtre**, avec deux exceptions
délibérées. `/admin/search-logs/summary` écarte les tours de page de
tout ce qui répond à « combien de recherches » : `total_searches`,
`unique_users`, `unique_ips`, `by_day`, `searches_by_group`. Il expose en
plus `total_logged`, le nombre de LIGNES du journal (tours de page
compris).

Échappent au filtre, et il ne faut pas « corriger » cela :

| Ce qui n'est pas filtré | Pourquoi |
|---|---|
| `feedback_up` / `feedback_down`, et les avis de `by_group` | Le pouce est rattaché à la dernière recherche affichée : un avis donné depuis la page 3 porte sur une ligne « page 3 ». L'écarter jetterait un avis réel, et la part positive est un rapport entre AVIS, pas entre recherches. |
| `timing` (`avg`, `p50`, `p95`, `took_avg`, `slow_count`, `measured`) | Un tour de page est une requête pleine et entière, et c'est en pagination profonde (`from` élevé) que le moteur est le plus lent : filtrer masquerait les requêtes lentes que ce panneau existe pour montrer. C'est pourquoi `measured` se rapporte à `total_logged` et non à `total_searches`. |

`/admin/search-logs/zero-results` n'est pas filtré non plus : une
recherche sans résultat n'a pas de page suivante à atteindre (le bouton
est désactivé), et une ligne « page N » à zéro résultat — un permalien
profond vers un jeu de résultats devenu vide — est un événement réel que
le panneau doit montrer.

**Journal du service.** Une recherche dont `duration_ms` atteint
`SLOW_SEARCH_MS` (2000 par défaut, `0` désactive) laisse une ligne
`WARNING` dans `journalctl -u docsearch-api`. Ce seuil doit rester aligné
avec la macro Zabbix `{$DOCSEARCH.RECHERCHE.MS.MAX}`
(`docsearch-infra/zabbix/REFERENCE.md`), faute de quoi la supervision
alerte sur des recherches dont le journal ne dit rien. Toutes les autres
recherches sont tracées en `DEBUG` : `LOG_LEVEL=DEBUG` les fait
apparaître le temps d'une observation, sans rien émettre le reste du
temps.

⚠️ `search_api.py` appelle `logging.basicConfig()` au chargement. Sans
lui, le logger racine n'a aucun handler — uvicorn ne configure que les
siens — et tous les `logger.info`/`logger.debug` de l'application
partaient dans le vide. Ne pas le retirer en croyant à un doublon avec
uvicorn : ses loggers ne propagent pas.

L'affichage de la durée côté interface est une bascule d'administration
(`search_time_enabled`), désactivée par défaut, doublée d'une préférence
par poste. La mesure, elle, a lieu quel que soit ce réglage.

## Historique personnel et autocomplétion

`GET /me/searches` et `GET /search/suggest` (voir `user_history.py`) lisent
l'index `search_logs`, écrit à chaque recherche depuis toujours :
**aucune collecte nouvelle**, seulement la restitution à l'intéressé de
ce qui n'allait jusqu'ici qu'aux statistiques d'administration.

**Le nom d'utilisateur n'est jamais un paramètre** : il vient du jeton de
session. Il n'existe aucune route permettant de lire l'historique de
quelqu'un d'autre — la ventilation par utilisateur, c'est
`/admin/search-logs`, réservée aux administrateurs et tracée au journal
d'audit.

⚠️ **Les requêtes des autres utilisateurs ne sont jamais suggérées**, et
ce n'est pas un manque à combler : « les recherches les plus fréquentes »
est la variante tentante et fuyante de la même fonctionnalité. Une
requête porte régulièrement le nom d'un dossier, d'une affaire ou d'une
personne que son auteur est seul à connaître.

Les suggestions du corpus (`/search/suggest`) portent sur **l'auteur, les
mots-clés et les facettes SQL personnalisées**, filtrés par l'ACL de
l'appelant et les sources cherchables — exactement les filtres de
`/search`, passés à `user_history.py` par l'appelant plutôt que
reconstruits, pour qu'il n'existe qu'une seule définition de « ce que cet
utilisateur a le droit de voir ». Une agrégation divulgue autant qu'un
résultat de recherche.

### Facettes personnalisées suggérées (2026-08-13)

Toute colonne marquée « facette » dans le mapping d'une source SQL voit
ses valeurs proposées sous la barre, à côté des auteurs et des mots-clés,
et la proposition retenue coche cette facette-là. La suggestion porte donc
`kind: "custom"` **plus `field` et `label`** : sans le champ, l'interface
saurait qu'il faut cocher une facette, pas laquelle.

Trois garde-fous, chacun pour une raison distincte — voir
`_suggestable_custom_facets()` (search_api.py) et `champs_agregables()`
(user_history.py) :

- **Le nom d'une facette est un morceau du schéma de sa source** (« Motif
  de la sanction »), et se filtre donc par `allowed_groups` comme le reste
  de la source. Les *valeurs*, elles, sont couvertes par les filtres ACL
  déjà passés à l'agrégation.
- **Seuls les `keyword` sont suggérés.** Une facette a le droit d'être
  `boolean` (la barre latérale l'affiche), mais l'`include` d'une
  agrégation `terms` est une expression régulière, qu'Elasticsearch refuse
  hors des champs textuels — et « true » n'a rien à faire sous une barre
  de recherche.
- **Le type déclaré ne suffit pas : le moteur est interrogé** (`field_caps`,
  mémorisé 60 s, ~6 ms). Un index n'est remappé qu'à sa création : changer
  le type d'une colonne dans l'administration ne touche pas l'existant, et
  la configuration annoncerait un champ agrégeable là où l'index porte
  encore du texte. Deux sources déclarant le même nom de champ sous deux
  types produisent le même conflit sur l'alias. L'enjeu n'est pas la
  facette fautive : l'agrégation de corpus est **une seule requête**, et un
  champ mal typé la fait échouer entière — plus d'auteurs, plus de
  mots-clés, plus rien.

Les champs agrégés sont **plafonnés à six** (`MAX_CHAMPS_CUSTOM`) et les
propositions sont réparties **à tour de rôle** entre les champs : par
concaténation, un auteur prolifique remplissait les huit lignes et les
dernières facettes n'étaient jamais visibles. La règle « ce qui commence
par la saisie d'abord » passe avant le tour de rôle, tous champs
confondus.

Coût mesuré le 2026-08-13 sur la pile de développement (24 019 documents,
`took` d'ES, à chaud) : **1-4 ms** pour auteur + mots-clés seuls, **2-5 ms**
en ajoutant les trois facettes de la source `agents` — dont `telephone`,
qui compte 995 valeurs distinctes, soit une par agent. Le plafond n'est
donc pas là pour ce corpus-ci, mais pour la configuration qui marquerait
quinze colonnes en facette sur un index bien plus gros.

**Pourquoi pas le nom de fichier ni le titre** — mesuré le 2026-08-12 sur
la pile de développement (23 016 documents) : le coût d'un `include`
régex tient au balayage du dictionnaire de termes, donc à la
**cardinalité** du champ. 151 auteurs et 102 mots-clés distincts, contre
22 494 noms de fichier, soit un par document. Les deux premiers restent
bornés quand le corpus grandit, le troisième croît avec lui. Suggérer des
noms de fichier suppose un champ dédié (`search_as_you_type` ou
`completion`), donc une réindexation — voir
`docsearch-infra/PLAN-EVOLUTIONS.md`.

`/search/suggest` est **du meilleur effort de bout en bout** : moins de deux
caractères, panne d'Elasticsearch ou dépassement du délai (300 ms)
renvoient la liste constituée jusque-là, jamais une erreur. Une barre de
recherche qui affiche « 503 » sous les doigts serait pire que pas de
suggestion.

Les deux fonctionnalités sont suspendables depuis l'administration
(`search_history_enabled`, `autocomplete_enabled`) et **démarrent
désactivées** — comme `search_time_enabled`, et pour la même raison :
elles ajoutent un élément à l'écran, elles ne masquent rien d'existant.
Désactivées, les routes renvoient 403.

⚠️ L'historique est borné par la **conservation des journaux** (voir plus
bas) : une recherche vieille de plus de `retention_search_logs_days` a
disparu de l'index, donc de l'historique de son auteur. C'est cohérent —
c'est la même donnée — mais ça se dit.

Une recherche **sans texte libre** (filtres seuls) n'entre pas dans
l'historique : elle s'y afficherait comme une ligne vide, et le format
n'en porte pas de quoi la rejouer — `search_logs` enregistre les critères
à titre informatif, mais pas les facettes personnalisées des sources SQL.

### Effacement par l'utilisateur (2026-08-14)

`DELETE /me/searches` et `DELETE /me/recent-documents` vident les deux
listes personnelles. Chacune de son côté : effacer ce qu'on a cherché et
effacer ce qu'on a ouvert sont deux gestes distincts.

Les deux **réécrivent le journal** et aucune ne se contente de masquer.
Mais elles ne le réécrivent pas de la même façon — l'une anonymise,
l'autre supprime — et cet écart suit l'imbrication des données : le clic
vit DANS la recherche, la recherche ne vit dans rien (voir
`history_purge.py`).

**`DELETE /me/searches` anonymise le journal.** La route ôte des
recherches antérieures à l'appel ce qui **nomme** leur auteur —
`username` et `ip` — par un `_update_by_query` sur `search_logs`. Tout
le reste demeure : texte cherché, résultats, temps, avis pouce, clics.
La recherche continue de compter dans les statistiques de
l'installation ; elle ne nomme plus personne. **C'est irréversible.**
Anonymiser plutôt que supprimer, pour deux raisons :

1. Le même document porte les statistiques d'administration, l'avis
   pouce et la trace d'exploitation. Ranger son écran n'est pas décider
   de la comptabilité de l'installation.
2. La suppression pour de bon existe déjà, décidée et écrite ailleurs :
   la **conservation des journaux** (voir plus bas). Une seconde voie de
   suppression, à la main de chacun, brouillerait ce qui est un délai.

⚠️ **`groups` n'est PAS ôté** (arbitré le 2026-08-14) : un groupe est un
service, pas quelqu'un, et les répartitions par service sont une lecture
réellement utilisée. Réserve, déjà écrite dans l'aide des statistiques :
aucun effectif minimum n'est appliqué à ces répartitions, donc dans un
service très restreint, un groupe et une requête singulière peuvent
suffire à resserrer sur une personne. L'anonymisation ôte le nom, elle ne
fabrique pas un anonymat statistique — et l'écran ne promet que cela.

⚠️ **Conséquence assumée** : les clics sont `nested` **dans le document
de la recherche** qui les a produits. Anonymiser ses recherches détache
donc aussi ses documents consultés, et `/me/recent-documents` perd tout
ce qui précède l'effacement. On ne peut pas rendre une recherche anonyme
en gardant nominatif le clic qu'elle porte. L'interface l'annonce avant
de confirmer.

**`DELETE /me/recent-documents` supprime le détail des clics** antérieurs
à l'appel — `doc_id`, `timestamp`, `position` — dans les recherches de
l'appelant, et reporte leur nombre dans `clicks_erased` (voir
`search_log.py`). La borne porte sur la **date du clic**, pas sur celle
de la recherche : c'est ce que cette liste-là raconte, et un document
ouvert ce matin depuis une recherche du mois dernier s'efface donc sans
que la recherche bouge. **C'est irréversible.**

Supprimer et non anonymiser, cette fois, parce que ce qui rattache un
clic à quelqu'un n'est pas dans le clic : c'est le `username` de la
recherche qui le contient. L'anonymiser emporterait un historique de
recherche que l'utilisateur n'a **pas** demandé d'effacer — entre
détruire ce qu'il demande d'effacer et détruire ce qu'il ne demande pas,
le choix est fait. La contrepartie du geste inverse, elle, est
inévitable : on peut retirer un clic d'une recherche nommée, on ne peut
pas nommer un clic dans une recherche anonyme.

Le **nombre** subsiste parce que « cette recherche a mené à trois
consultations » est le signal d'engagement que l'installation lit
vraiment (colonne « Clics », export XLS). Le faire tomber à zéro ferait
passer ces recherches pour infructueuses — un journal qui ment par
omission ne vaut pas mieux qu'un écran qui ment. L'interface affiche donc
« 3 (dont 2 effacés) », et l'export porte la part effacée en dernière
colonne.

Les marqueurs continuent d'être posés et lus, mais en **second rideau** :
ils couvrent les effacements demandés avant cette version et l'événement
journalisé à la seconde près, encore invisible du moteur au moment de la
réécriture. Celui des recherches est lu par `/me/searches` **et
`/search/suggest`**, qui puise au même historique et ressusciterait sinon
sous les doigts ce qui vient d'être effacé.

Pannes : à la lecture, Redis injoignable retombe sur « jamais effacé »
(historique complet — la panne d'un cache ne fait pas disparaître ses
recherches à quelqu'un qui n'a rien demandé), ce qui est sans danger,
les traces ayant déjà été réécrites. À l'écriture, un journal impossible
à réécrire répond **503** plutôt que de faire semblant ; le marqueur, lui,
est en meilleur effort une fois la réécriture faite — échouer là-dessus
annoncerait un échec à quelqu'un dont les traces sont bel et bien
parties. Les deux routes suivent les bascules des listes qu'elles
effacent : désactivées, elles renvoient 403 comme les `GET`.

## Collections partagées et documents récemment consultés

**`GET /me/recent-documents`** relit les clics déjà journalisés (voir
`POST /click`) pour en tirer les derniers documents ouverts par
l'appelant. Les identifiants viennent du journal, mais les **documents
sont relus à travers le filtre ACL** : un document dont les droits ont
changé depuis la consultation, ou supprimé de l'index, n'est pas rendu.
Un historique de consultation ne rouvre pas une porte qui s'est fermée.
La liste s'efface par `DELETE /me/recent-documents` — voir « Effacement
par l'utilisateur » plus haut.

**`POST /collections/{id}/share`** partage une collection avec des
groupes (liste vide = retour au personnel). Trois règles :

1. ⚠️ **Partager donne la RÉFÉRENCE, pas le droit de lecture.** Une
   collection ne stocke que des identifiants ; chaque document est relu
   par `GET /document/{id}`, qui applique l'ACL. Deux personnes ouvrant
   la même collection n'y voient donc pas forcément le même nombre de
   documents — l'interface l'annonce (« 3 documents ne vous sont pas
   accessibles ») plutôt que de masquer l'écart, sans quoi le
   propriétaire croit avoir partagé dix documents quand le destinataire
   en voit sept.
2. **Seul le propriétaire écrit.** Renommer, ajouter, retirer,
   supprimer et repartager lui restent réservés — pas de verrouillage à
   écrire. Le destinataire, lui, **duplique**
   (`POST /collections/{id}/duplicate`) : la copie lui appartient.
3. **On ne partage qu'avec un groupe dont on est soi-même membre.** Sans
   cette borne, le premier usage serait de pousser une collection à
   toute l'organisation.

Suspendable par `collections_shared_enabled` : désactivé, les
collections partagées cessent d'apparaître chez leurs destinataires sans
être modifiées — le réglage se rallume et tout revient.

## Aide au zéro résultat

Quand `/search` ne renvoie **aucun** résultat, sa réponse gagne un bloc
`zero_result` — absent dans tous les autres cas, y compris quand il n'y a
rien à proposer :

```json
"zero_result": {
  "suggestion": "rapport annuel",
  "relaxations": [{"field": "extension", "count": 12}, {"field": "__all__", "count": 40}],
  "sources": [{"key": "archives", "doc_count": 7}]
}
```

- **`suggestion`** : la requête corrigée (`term suggester` sur `content`,
  en `suggest_mode: popular` — ne propose qu'un terme plus fréquent que
  celui tapé). Les positions rapportées par Elasticsearch sont respectées
  pour reconstruire la phrase : un `str.replace` toucherait aussi les
  occurrences d'un mot à l'intérieur d'un autre.
- **`relaxations`** : ce que donnerait le retrait d'UN filtre, un par
  entrée, plus `__all__` pour « tous les filtres » (proposé seulement à
  partir de deux). `field` vaut une dimension de facette, `custom:<champ>`,
  `date` ou `has_attachments`.
- **`sources`** : les sources non sélectionnées où il y a quelque chose.

⚠️ **Chaque compte annoncé est atteignable.** Les filtres ACL et de
sources cherchables ne sont jamais relâchés : annoncer « 12 résultats »
puis afficher une liste vide après le clic coûterait plus de confiance
qu'un écran vide honnête.

⚠️ **La correction ne fuit pas.** Le correcteur d'Elasticsearch travaille
sur le dictionnaire de termes de l'index, que l'ACL ne filtre pas : un mot
tiré d'un document interdit pourrait être proposé. La correction n'est
donc rendue que si elle donne des résultats **visibles par cet
utilisateur** — ce qui la débarrasse du même coup des corrections qui ne
mènent nulle part.

Tout ceci tient dans un seul `msearch`, exécuté **uniquement** quand le
total est nul : une recherche qui trouve quelque chose ne paie rien pour
cette aide. Une panne d'Elasticsearch pendant ce calcul rend un bloc
absent, pas une erreur — l'écran retombe alors sur son message d'origine.

## Doublons et thésaurus

Deux fonctionnalités qui s'appuient sur le **mapping** des index de
documents, donc sur `docsearch-ingestion` — voir son README pour
l'empreinte de contenu (`content_sha256`), les trois analyseurs du champ
`content` et les deux commandes de migration
(`./manage.sh migrer-synonymes`, `./manage.sh backfill-hashes`).

Côté API :

- `GET /admin/duplicates?source=…` regroupe les documents par empreinte
  et chiffre la place occupée par les copies. **Rapport administratif**,
  sans filtre ACL — comme la volumétrie ou la répartition par extension,
  et donc réservé au groupe d'administration. Servi depuis un **cache
  quotidien** : sans lui, chaque ouverture du panneau lancerait une
  agrégation sur tout l'index pendant que les utilisateurs cherchent.
  `rafraichir=true` force le recalcul.
- `GET/POST/DELETE /admin/synonyms` gère le jeu de règles. Chaque
  écriture remonte le **nombre de shards rechargés** par Elasticsearch :
  c'est la seule preuve que la règle est en vigueur, le rechargement
  étant fait par le moteur lui-même, sans réindexation.
- `POST /admin/synonyms/test` rend les jetons produits par l'analyseur de
  recherche. Indispensable : une règle mal écrite ne produit aucune
  erreur, seulement une recherche qui ne trouve rien de plus qu'avant.

### Résultats épinglés

`GET/POST /admin/pinned` (voir `pinned.py`) associe une requête
normalisée — minuscules, accents repliés, espaces réduits — à quelques
identifiants de documents, dans un registre Redis. Sur la **première page
seulement**, `/search` les rend dans une clé `pinned` distincte de
`results`.

⚠️ **Un document épinglé n'échappe pas à l'ACL.** Il est relu par une
vraie recherche portant le filtre ACL et la restriction aux sources
cherchables — jamais par un `mget`, qui le rendrait sans rien vérifier.
Épingler met en avant, ça n'autorise pas : celui qui n'a pas le droit de
voir le document ne le voit pas, et rien à l'écran ne lui apprend qu'il
existe.

Trois conséquences à connaître :

- un document épinglé présent dans les résultats naturels en est retiré,
  pour n'être affiché qu'une fois. `total` ne bouge pas : il compte des
  documents trouvés, pas des cartes affichées ;
- l'ordre rendu est celui de l'administration, pas celui du moteur —
  quand quelqu'un épingle trois documents, il les a classés ;
- un document supprimé de l'index disparaît de lui-même côté recherche.
  C'est `GET /admin/pinned` qui le signale comme introuvable, pour qu'on
  puisse nettoyer la règle — sans quoi on épingle durablement un lien
  mort que personne ne voit disparaître.

L'interface l'affiche sous une mention « Proposé par votre
administration » : un classement forcé en silence est une mauvaise
surprise le jour où quelqu'un s'en aperçoit.

La forme `a => b` est refusée à l'écriture : elle *remplace* les termes
d'origine au lieu de les compléter, ce qui surprend tout le monde et se
règle très mal depuis une interface. L'équivalence (`a, b`) couvre le
besoin réel.

## Conservation des journaux

Cinq index de journalisation grandissaient sans aucune limite :
`search_logs`, `login_events`, `admin_audit_log`, `nps_responses` et
`suggestions`. `log_retention.py` les purge **une fois par jour**, depuis
le tick d'`alert_worker.py` — pas de conteneur de plus, pas
d'ordonnanceur système : ce processus tourne déjà et porte la même image.

Deux raisons, et la seconde est la plus importante : au-delà du
flood-stage watermark (95 % de disque), Elasticsearch passe ses index en
lecture seule pendant que le cluster reste « green » et que les voyants
d'administration restent au vert (c'est arrivé le 2026-08-10) ; et sur un
service de l'État, la durée de conservation de données personnelles —
identifiant, texte des recherches, adresse IP — est une décision qui se
prend et se tient, pas une conséquence de la taille du disque.

| Paramètre (`/admin/config`) | Défaut | Pourquoi |
|---|---|---|
| `retention_search_logs_days` | 365 | comparaison d'une année sur l'autre |
| `retention_login_events_days` | 365 | trace de sécurité |
| `retention_audit_log_days` | 1095 | la trace qui protège l'administrateur se garde plus longtemps que ce qu'elle trace |
| `retention_nps_days` | 730 | tendance de satisfaction |
| `retention_suggestions_days` | 730 | porte un `username` quand la suggestion n'est pas anonyme |

`0` signifie **conservation illimitée**, et une valeur illisible vaut
`0` : sur un mécanisme qui supprime, le doute profite à la conservation.
`GET /admin/retention` montre, journal par journal, ce que la purge
emporterait — un réglage destructeur qu'on ne peut pas prévisualiser ne
se règle jamais, ou se règle une fois de trop.

**Ce qui n'est jamais touché** : `custom_keywords` et `saved_collections`
sont des données utilisateur, pas des traces. La liste est explicite et
close — jamais un motif du genre `*_logs`, qui emporterait un jour l'index
de quelqu'un d'autre.

Chaque passage est borné (`RETENTION_MAX_DOCS`, 100 000 par journal) et
bridé (`RETENTION_REQUESTS_PER_SECOND`) : une première purge sur une
installation ancienne ne doit pas occuper le cluster pendant des heures,
le reliquat partant le lendemain. Le nombre de documents supprimés va dans
le journal du service, et **la purge du journal d'audit s'inscrit
elle-même dans le journal d'audit**.

⚠️ `delete_by_query` ne rend pas le disque immédiatement : les segments ne
sont réécrits qu'à la fusion. Aucun `_forcemerge` n'est déclenché
automatiquement — c'est une opération lourde, qui reste une décision
d'exploitation.

## Alertes sur recherches sauvegardées

Une recherche enregistrée (`saved_searches.py`) peut être marquée
"alerte" (`PATCH /saved-searches/{id}/alert`, fréquence quotidienne ou
hebdomadaire). Un worker séparé, `alert_worker.py` — conteneur
`docsearch-alert-worker` (unité Quadlet de `docsearch-infra`), même image que
`api` mais aucune route HTTP exposée — rejoue périodiquement les
critères de chaque recherche marquée, restreints aux documents dont
`indexed_at` (date d'entrée dans l'index, pas `date_modified`) est
postérieure à la dernière vérification. S'il trouve de nouveaux
résultats, une notification est déposée dans Redis
(`alert_notifications.py`) et lue par l'interface via `GET /alerts`.

**In-app uniquement, pas d'email** : DocSearch n'a aujourd'hui aucune
brique SMTP, et un email ferait sortir des titres de documents
potentiellement confidentiels (filtrés par ACL à l'intérieur de l'app)
hors du périmètre d'accès contrôlé. Suspendable globalement depuis
l'admin (`ui_config.alerts_enabled`), comme les collections et les
mots-clés personnalisés — désactivé, toutes les routes `/alerts*` et
`PATCH /saved-searches/{id}/alert` renvoient 403.

`search_query.py` reconstruit volontairement sa propre version (must +
filtres ACL/facettes) de la requête ES de `/search`, plutôt que
d'importer `search_api.py` dans le worker — ce dernier charge FastAPI,
Kafka et LDAP au chargement du module, inutilement lourd pour un simple
worker de fond. ⚠️ Cette duplication doit rester en cohérence avec la
construction de requête de `/search` : toute évolution de la logique de
filtrage faite dans `search_api.py` doit être répercutée dans
`search_query.py`, sinon une alerte pourrait signaler des documents
qu'une recherche manuelle ne trouverait pas (ou l'inverse).

## Authentification

**Tout vit dans [`app/auth/`](app/auth/)** — architecture reprise de
`charlie/app-api-auth` ; les écarts et leur justification sont dans
`docsearch-infra/PLAN-AUTH-SSO.md`.

L'identité vient d'un **jeton RS256 signé par cette application**, posé en
cookie `httpOnly` à la connexion et vérifié à chaque requête
(`app/auth/deps.py::current_user`). Elle ne vient plus de l'en-tête
`X-User` : celui-ci était censé être injecté par Nginx après validation
SSO, mais le SSO n'a jamais été branché et l'API publiant son port,
`curl -H "X-User: alice.admin" …/admin/status` répondait `200`. `X-User`
subsiste comme harnais de développement, sous `TRUST_X_USER_HEADER`, et
l'API **refuse de démarrer** s'il est armé avec `API_ENV=production`.

| Route | Rôle |
|---|---|
| `POST /auth/login` | `{identifiant, mot_de_passe}` → cookies de session. Le **serveur** choisit le fournisseur : l'existence d'un compte de secours local est le discriminant, et il n'y a aucun repli de l'un vers l'autre |
| `GET /auth/login/kerberos` | Connexion automatique par ticket SPNEGO (voir plus bas) |
| `POST /auth/refresh` | Renouvelle le jeton d'accès. Le jeton de rafraîchissement ne sert **qu'une fois** |
| `POST /auth/logout` | Révoque la session côté Redis — sans quoi « se déconnecter » n'effacerait qu'un cookie recollable |
| `GET /auth/me` | Identité, groupes effectifs, `is_admin` |
| `GET /auth/check-access`, `/auth/check-admin` | Cibles internes du `auth_request` de Nginx, qui garde chaque page |
| `GET /auth/.well-known/jwks.json` | Clé publique (RFC 7517) |

Régimes d'erreur, constants : `401` identifiants refusés (message
générique unique, jamais de variation qui dirait lequel des deux est en
cause), `403` authentifié mais hors du groupe requis, `429` trop de
tentatives, `501` SSO désactivé, `503` annuaire / Redis / keytab / clés
indisponibles. **Un 503 n'est jamais présenté comme un 401** : une panne
déguisée en mot de passe faux envoie chercher au mauvais endroit.

Prérequis, une fois : `scripts/generer-cles.py` (les clés vivent hors du
dépôt et hors de l'image). Le dossier étant monté **en lecture seule**
dans le service, la génération passe par un conteneur jetable :

```bash
sudo install -d -o 1000 -g 1000 -m 700 /etc/docsearch/jwt
sudo podman run --rm -v /etc/docsearch/jwt:/etc/docsearch/jwt:Z \\
     localhost/docsearch/api:latest python scripts/generer-cles.py
```

### Comptes de secours locaux

`scripts/gerer-comptes-locaux.py`, jamais une route HTTP. Ce **n'est pas**
une gestion d'utilisateurs : sans annuaire, `require_access` refuse tout
le monde, administration comprise — ces comptes sont la porte de secours,
et ils **portent leurs propres groupes**, sans quoi ils se feraient
refuser par le contrôle qu'ils sont censés contourner.

### Connexion automatique Kerberos / SPNEGO

`app/auth/kerberos.py`, transposé de `charlie/app-api-auth`. Désactivé par
défaut (réglage à chaud `sso_kerberos_enabled`, panneau
d'administration) : sans interrupteur, une installation sans keytab
répondrait un défi que personne ne peut relever, à chaque chargement de
page.

Ce qui décide du succès n'est pas le code : un FQDN (le navigateur dérive
le SPN du nom d'hôte — il ne tente **rien** contre une IP littérale), un
SPN `HTTP/<fqdn>`, un keytab, un certificat au même nom, et une stratégie
de parc autorisant les navigateurs à envoyer un ticket. Voir
`PLAN-AUTH-SSO.md` §2.5.

## ACL

Chaque requête de recherche est filtrée automatiquement, à partir des
**groupes effectifs** (annuaire ∪ compte de secours,
`app/auth/directory.py::get_effective_groups` — point unique de vérité) :

```python
acl_filter = {
    "bool": {
        "should": [
            {"term":  {"acl.public": True}},
            {"term":  {"acl.owner":  username}},
            {"term":  {"acl.users":  username}},
            {"terms": {"acl.groups": user_groups}},  # POSIX + LDAP/AD
        ],
        "minimum_should_match": 1
    }
}
```

### Quelles sources un utilisateur peut atteindre

Orthogonal à l'ACL par document ci-dessus : celle-là filtre les documents
d'une source visible, celle-ci masque une source **en bloc** (bascule
`searchable`, ou restriction `allowed_groups` à des groupes AD/LDAP).

La règle est **unique** et vit dans `app/docsearch_contract/sources.py`,
appelée via `app/source_registries.py` — par `search_api.py` comme par
`search_query.py` (le worker d'alertes). Elle était auparavant écrite six
fois, dont deux avec le commentaire « Identique à… » : une divergence
entre la copie que lit `/search` et celle que lit le worker faisait
notifier une alerte sur une source que l'écran n'affiche plus.

⚠️ `app/docsearch_contract/` est une **copie générée** de
`docsearch-infra/contract/` — jamais modifiée sur place. Y porter une
modification, puis `./manage.sh sync-contract` depuis `docsearch-infra`,
qui la recopie ici ; `./manage.sh build` refuse de construire tant qu'une
copie diverge, et `tests/test_contrat_vendorise.py` le vérifie aussi.

## Panneau d'administration (/admin)

Routes protégées par appartenance à un groupe LDAP/AD (`ADMIN_GROUP`,
nécessite `LDAP_ENABLED=true`) — voir `admin_auth.py`. Interface web
correspondante : `docsearch-ui-vue/admin.html` (+ `src/pages/admin/`).

| Route | Rôle |
|---|---|
| `GET /admin/status` | État de tous les composants (ES, Redis, Tika, Kafka, workers actifs, progression de l'indexation, battement du watcher) |
| `GET /metrics` | Métriques d'indexation (documents indexés, taille de l'index, répartition par extension) — route publique existante, réutilisée par le panneau admin |
| `GET/POST/DELETE /admin/file-sources[/{name}]`, `.../label`, `.../description`, `.../ocr` | Sources fichiers : CRUD, libellé, description, activation de l'OCR par source |
| `GET/POST/DELETE /admin/sql-sources[/{name}]`, `.../label`, `.../description` | Sources SQL (PostgreSQL/MySQL) |
| `GET/POST/DELETE /admin/sql-dsns[/{name}]` | DSN chiffrés (Fernet) utilisables par les sources SQL |
| `GET/POST/DELETE /admin/web-sources[/{name}]`, `.../label`, `.../description`, `.../pause` | Sources web (Elastic Open Web Crawler) |
| `GET /admin/all-sources`, `POST .../searchable`, `.../collectable` | Vue unifiée fichier/SQL/web — bascules "Recherche"/"Collections", par source |
| `GET/POST /admin/filetypes`, `POST .../reset` | Types de fichiers indexés (activation, taille max), par source |
| `GET/POST /admin/config`, `POST .../reset` | Paramètres opérationnels (limites d'archives, cadences, OCR) |
| `GET/POST /admin/path-filters`, `.../exclude`, `.../include`, `.../remove` | Inclusion/exclusion de sous-dossiers |
| `POST /admin/purge-path` | Purger l'index existant selon un motif (dry-run par défaut) |
| `POST /admin/ui-config` | Bascules d'interface (liens Assistant IA/Administration, export, collections...) — voir `GET /ui-config` public |
| `POST /admin/engagement-config` | Bascules de mesure de satisfaction (pouce, NPS, suggestions) — voir `GET /engagement-config` public |
| `GET /admin/nps-summary`, `.../suggestions`, `POST .../suggestions/{id}/status`, `DELETE .../suggestions/{id}` | Résultats NPS et suggestions utilisateurs (le `DELETE` efface définitivement — le statut, lui, ne sert qu'au suivi) |
| `GET /admin/search-logs[...]`, `.../summary`, `.../zero-results`, `.../export`, `GET /admin/audit-log` | Journaux de recherche et d'audit — alimentent `stats.html`. `exclude_pagination=true` écarte les tours de page (voir « Recherches véritables » ci-dessous), sur la liste comme sur l'export |
| `POST /admin/scan` | Déclencher un scan d'indexation (en arrière-plan) |

**Aucune de ces routes n'a besoin d'accéder au moteur de conteneurs** : l'état est
vérifié via le réseau applicatif normal (HTTP, Redis, Kafka — comme
un client classique), et le déclenchement de scan publie simplement
sur Kafka (les workers déjà actifs font le travail). Piloter le nombre
de workers ou démarrer/arrêter des conteneurs reste réservé à
`manage.sh` en CLI (`docsearch-infra`).

### Tester sans annuaire

`ADMIN_AUTH_DISABLED=true` contourne le contrôle de **groupe** sur
`/admin/*`. Il ne dispense plus d'être authentifié — c'est la différence
avec son comportement précédent, où il ouvrait aussi le panneau à un
anonyme complet.

⚠️ **Jamais en production**, et ce n'est plus une simple recommandation :
avec `API_ENV=production`, l'API **refuse de démarrer** si ce drapeau (ou
l'un des quatre autres harnais) est armé, plutôt que de l'ignorer — voir
`app/auth/guardrails.py` et `docsearch-infra/HOWTO-simuler-utilisateur.md`.
Hors production, un encadré s'affiche au démarrage et chaque usage laisse
une ligne de log.

**Modules dupliqués depuis `docsearch-ingestion`** (architecture
multi-dépôts : impossible d'importer le code d'un autre dépôt au
build) — `filetype_config.py`, `runtime_config.py`, `path_filter.py`
doivent rester identiques entre les deux dépôts. Redis reste la seule
source de vérité partagée, donc pas de risque de désynchronisation des
*données* — seul le *code* doit être maintenu en parallèle.

## Statistiques par groupe d'utilisateurs

Les journaux enregistrent, **au moment de l'événement**, les groupes
LDAP de l'utilisateur dans un champ `groups` (`keyword`) :

| Index | Écrit par | Remarque |
|---|---|---|
| `search_logs` | `search_log.log_search()` | Écrit dès la recherche, **pas** à l'avis : `POST /feedback` est une mise à jour partielle du même document, y attacher les groupes n'en aurait couvert que les recherches notées |
| `nps_logs` | `nps_log.log_nps()` | |
| `suggestions` | `suggestion_log.log_suggestion()` | **Uniquement si un `username` est présent** — une suggestion déposée anonymement ne reçoit pas de groupe, sans quoi l'anonymat choisi par son auteur serait percé |

Le mapping est ajouté par `put_mapping`, qui **fusionne sans écraser** :
les documents déjà indexés restent en place, simplement dépourvus du
champ. Ils tombent alors dans un lot `__sans_groupe__`, rendu « Non
renseigné » à l'écran — jamais ignoré silencieusement, un total par
groupe qui ne retombe pas sur le total global ferait douter de tout le
tableau.

**Restitution** — les endpoints existants sont étendus, aucun nouveau :
`GET /admin/search-logs/summary` (clés `searches_by_group` et
`by_group`, celle-ci portant avis positifs/négatifs par groupe),
`.../zero-results`, `GET /admin/nps-summary` (score **recalculé** par
groupe : `%promoteurs − %détracteurs`, jamais moyenné depuis le score
global) et `GET /admin/suggestions`.

`GET /admin/search-logs/export` porte une colonne **« Groupes »**,
juste après « Utilisateur ».

Deux propriétés à connaître avant de lire ces chiffres, rappelées sur
`stats.html` :

- un utilisateur appartenant à plusieurs groupes compte dans chacun —
  **la somme des lignes dépasse le total**, c'est correct ;
- **aucun seuil d'anonymat** n'est appliqué : dans un groupe très
  restreint, « ce groupe a mis 0 » désigne quelqu'un. Les consultations
  d'administration sont tracées dans le journal d'audit.

### Rétro-remplir l'historique (opération exceptionnelle)

`backfill_groups.py` complète les documents antérieurs à l'ajout du
champ, sans quoi le lot « Non renseigné » écrase tous les autres
pendant des mois :

```bash
sudo podman exec docsearch-api python3 backfill_groups.py          # simulation
sudo podman exec docsearch-api python3 backfill_groups.py --apply  # écriture
```

⚠️ **Sémantique inverse de la capture normale** : le script applique
l'appartenance LDAP **d'aujourd'hui** à des événements passés. Un agent
ayant changé de service voit ses anciennes recherches recomptées dans
son service actuel. Acceptable une fois pour amorcer les statistiques,
à ne pas transformer en tâche récurrente — rejoué régulièrement, il
réécrirait l'histoire à chaque mouvement de personnel.

Garde-fous : simulation par défaut ; ne touche **que** les documents
dépourvus de `groups` (une valeur capturée à l'écriture fait foi et
n'est jamais écrasée) ; laisse intactes les suggestions anonymes ; et
n'écrit rien pour un utilisateur introuvable dans LDAP — une liste vide
masquerait le fait qu'on n'a rien trouvé.

⚠️ Sur les instances déjà en service, l'index `suggestions` peut porter
un `username` de type `text` : il précède la déclaration en `keyword`
de `suggestion_log.py`, et Elasticsearch ne change jamais le type d'un
champ existant. Toute agrégation dessus échoue (« Fielddata is
disabled ») ; seule une réindexation corrigerait. Le script détecte le
cas et bascule sur le sous-champ `username.keyword`.

## Lancer en local (nécessite un ES déjà peuplé)

```bash
podman build -t localhost/docsearch/api:latest .
podman run -p 8000:8000 --env-file /etc/docsearch/docsearch.env \
  --network docsearch-net \
  localhost/docsearch/api:latest

curl http://localhost:8000/health
open http://localhost:8000/docs   # Swagger UI
```

## Activer LDAP/Active Directory

```bash
# Dans .env
LDAP_ENABLED=true
LDAP_HOST=ldaps://votre-dc.domaine.gouv.fr
LDAP_BASE=dc=domaine,dc=gouv,dc=fr
LDAP_BINDDN=cn=svc-docsearch,ou=services,dc=domaine,dc=gouv,dc=fr
LDAP_PASS=...
```

**`ldaps://` et non `ldap://`** : le bind en clair est désormais refusé,
sauf dérogation explicite `LDAP_ALLOW_PLAINTEXT_INSECURE=true`, qui reste
possible (beaucoup d'annuaires internes n'exposent pas LDAPS, et en faire
une erreur fatale couperait l'application au lieu de la sécuriser) mais
journalise un `WARNING` à chaque connexion. **Une installation existante
qui bindait en clair doit poser ce drapeau au moment de la mise à jour**,
sinon plus personne ne se connecte.

`ldap3` est une implémentation Python pure — aucune dépendance système
(pas besoin de `libldap-dev`).

## Linter

```bash
.venv/bin/pip install -r requirements-dev.txt   # une fois
.venv/bin/ruff check .                          # signale
.venv/bin/ruff check --fix .                    # corrige ce qui est sûr
```

`ruff` tournait déjà ici : `.github/workflows/ci.yml` lance `ruff check app/`
depuis toujours. Ce qui a changé le 2026-08-12, c'est qu'il a désormais un
`ruff.toml` — jusque-là il tournait sur ses seules règles par défaut (E4, E7,
E9, F), sans que ce choix soit écrit nulle part. Le fichier le rend explicite,
ajoute `B`, `C4` et `SIM`, étend l'analyse à `tests/`, et argumente règle par
règle ce qui reste écarté :

- **`E402`** — les constantes d'environnement se lisent délibérément ENTRE les
  imports (`ES_HOST`, `REDIS_HOST`, `LDAP_ENABLED` en tête de `app/search_api.py`),
  et `auth/config.py` lit l'environnement à l'import, ce dont `tests/conftest.py`
  dépend. 39 signalements pour un parti pris assumé.
- **`E701`** — les tables de correspondance alignées de `app/admin_scan.py`.
- **`B904`** — `raise HTTPException(...)` sans `from err` : 80 occurrences, un
  lot à traiter pour lui-même.
- **`E501`** — 383 signalements pour un 95e centile réel à 84 caractères. Aucun
  formateur n'est en place, et le dépôt n'en a jamais eu.

La version est **épinglée à l'identique dans `requirements-dev.txt` et dans la
CI** : un linter dont la version flotte finit par échouer en CI sur une règle
que personne n'a choisie.

## Tests

```bash
python -m pytest
```

Les tests LDAP tapent le **vrai** annuaire de dev de la VM
(`~/ldap-test-stack`) et se sautent proprement s'il est arrêté — ou si
ses deux mots de passe ne sont pas dans l'environnement, car ils ne sont
délibérément pas écrits dans le dépôt :

```bash
export DOCSEARCH_TEST_LDAP_BIND_PASSWORD=...   # cn=admin, voir son docker-compose.yml
export DOCSEARCH_TEST_LDAP_USER_PASSWORD=...   # alice.admin / bob.user, voir 03-users.ldif
```

Sans eux, 91 tests passent et 9 se sautent (`requires_ldap`) ; ceux de session tapent le **vrai** Redis
(`requires_redis`), sous le préfixe `docsearch:auth:` uniquement, nettoyé
avant et après chaque test. `requires_kerberos` marque le seul chemin
qu'aucune machine de ce projet ne peut exercer : l'acceptation d'un ticket
authentique, qui attend un KDC.
