"""

    Reynir: Natural language processing for Icelandic

    Built-in query module

    Copyright (C) 2019 Miðeind ehf.
    Original author: Vilhjálmur Þorsteinsson

       This program is free software: you can redistribute it and/or modify
       it under the terms of the GNU General Public License as published by
       the Free Software Foundation, either version 3 of the License, or
       (at your option) any later version.
       This program is distributed in the hope that it will be useful,
       but WITHOUT ANY WARRANTY; without even the implied warranty of
       MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
       GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see http://www.gnu.org/licenses/.


    This module implements a default query processor for builtin queries.
    The processor operates on queries in the form of parse trees and returns
    the results requested, if the query is valid and understood.

"""

import math
from datetime import datetime
from collections import defaultdict

from settings import Settings

from db.models import Article, Person, Entity, Root
from db.queries import RelatedWordsQuery, ArticleCountQuery, ArticleListQuery

from treeutil import TreeUtility
from reynir import TOK, correct_spaces
from reynir.bintokenizer import stems_of_token
from search import Search


# Indicate that this module wants to handle parse trees for queries
HANDLE_TREE = True

# Maximum number of top answers to send in response to queries
_MAXLEN_ANSWER = 20
# Maximum number of article search responses
_MAXLEN_SEARCH = 20
# If we have 5 or more titles/definitions with more than one associated URL,
# cut off those that have only one source URL
_CUTOFF_AFTER = 4
# Maximum number of URL sources so provide for each top answer
_MAX_URLS = 5
# Maximum number of identical mentions of a title or entity description
# that we consider when scoring the mentions
_MAX_MENTIONS = 5


def append_answers(rd, q, prop_func):
    """ Iterate over query results and add them to the result dictionary rd """
    for p in q:
        s = correct_spaces(prop_func(p))
        ai = dict(
            domain=p.domain,
            uuid=p.id,
            heading=p.heading,
            timestamp=p.timestamp,
            ts=p.timestamp.isoformat()[0:16],
            url=p.url,
        )
        rd[s][p.id] = ai  # Add to a dict of UUIDs


def name_key_to_update(register, name):
    """ Return the name register dictionary key to update with data about
        the given person name. This may be an existing key within the
        dictionary, the given key, or None if no update should happen. """

    if name in register:
        # The exact same name is already there: update it as-is
        return name
    # Look for alternative forms of the same name
    # These are all the same person, respectively:
    # Dagur Bergþóruson Eggertsson  / Lilja Dögg Alfreðsdóttir
    # Dagur B. Eggertsson           / Lilja D. Alfreðsdóttir
    # Dagur B Eggertsson            / Lilja D Alfreðsdóttir
    # Dagur Eggertsson              / Lilja Alfreðsdóttir
    nparts = name.split()
    mn = nparts[1:-1]  # Middle names
    # Check whether the same person is already in the registry under a
    # slightly different name
    for k in register.keys():

        parts = k.split()
        if nparts[0] != parts[0] or nparts[-1] != parts[-1]:
            # First or last names different: we don't think these are the same person
            # !!! TODO: Could add Levenshtein distance calculation here
            continue

        # Same first and last names
        # If the name to be added contains no middle name, it is judged to be
        # already in the register and nothing more needs to be done
        if not mn:
            return k  # We can just update the key that was already there
        mp = parts[1:-1]  # Middle names
        if not mp:
            # The new name has a middle name which the old one didn't:
            # Assume its the same person but modify the registry key
            assert name != k
            register[name] = register[k]
            del register[k]
            return name  # No update necessary

        # Both have middle names

        def has_correspondence(n, nlist):
            """ Return True if the middle name or abbreviation n can
                correspond to any middle name or abbreviation in nlist """
            if n.endswith("."):
                n = n[:-1]
            for m in nlist:
                if m.endswith("."):
                    m = m[:-1]
                if n == m:
                    return True
                if n.startswith(m) or m.startswith(n):
                    return True
            # Found no correspondence between n and nlist
            return False

        c_n_p = [has_correspondence(n, mp) for n in mn]
        c_p_n = [has_correspondence(n, mn) for n in mp]
        if all(c_n_p) or all(c_p_n):
            # For at least one direction a->b or b->a,
            # all middle names that occur have correspondences
            if len(mn) > len(mp):
                # The new name is more specific than the old one:
                # Assign the more specific name to the registry key
                register[name] = register[k]
                del register[k]
                return name
            # Return the existing key
            return k

        # There is a non-correspondence between the middle names,
        # so this does not look like it's the same person.
        # Continue searching...

    # An identical or corresponding name was not found:
    # update the name key
    return name


def append_names(rd, q, prop_func):
    """ Iterate over query results and add them to the result dictionary rd,
        assuming that the key is a person name """
    for p in q:
        s = correct_spaces(prop_func(p))
        ai = dict(
            domain=p.domain,
            uuid=p.id,
            heading=p.heading,
            timestamp=p.timestamp,
            ts=p.timestamp.isoformat()[0:16],
            url=p.url,
        )
        # Obtain the key within rd that should be updated with new
        # data. This may be an existing key, a new key or None if no
        # update is to be performed.
        s = name_key_to_update(rd, s)
        if s is not None:
            rd[s][p.id] = ai  # Add to a dict of UUIDs


def make_response_list(rd):
    """ Create a response list from the result dictionary rd """
    # rd is { result: { article_id : article_descriptor } }
    # where article_descriptor is a dict

    # We want to rank the results roughly by the following criteria:
    # * Number of mentions
    # * Newer mentions are better than older ones
    # * If a result contains another result, that ranks
    #   as a partial mention of both
    # * Longer results are better than shorter ones

    def contained(needle, haystack):
        """ Return True if whole needles are contained in the haystack """
        return (" " + needle.lower() + " ") in (" " + haystack.lower() + " ")

    def sort_articles(articles):
        """ Sort the individual article URLs so that the newest one appears first """
        return sorted(articles.values(), key=lambda x: x["timestamp"], reverse=True)

    def length_weight(result):
        """ Longer results are better than shorter ones, but only to a point """
        return min(math.e * math.log(len(result)), 10.0)

    now = datetime.utcnow()

    def mention_weight(articles):
        """ Newer mentions are better than older ones """
        w = 0.0
        newest_mentions = sort_articles(articles)[0:_MAX_MENTIONS]
        for a in newest_mentions:
            # Find the age of the article, in whole days
            age = max(0, (now - a["timestamp"]).days)
            # Create an appropriately shaped and sloped age decay function
            div_factor = 1.0 + (math.log(age + 4, 4))
            w += 14.0 / div_factor
        # A single mention is only worth 1/e of a full (multiple) mention
        if len(newest_mentions) == 1:
            return w / math.e
        return w

    scores = dict()
    mention_weights = dict()

    for result, articles in rd.items():
        mw = mention_weights[result] = mention_weight(articles)
        scores[result] = mw + length_weight(result)

    # Give scores for "cross mentions", where one result is contained
    # within another (this promotes both of them). However, the cross
    # mention bonus decays as more crosses are found.
    CROSS_MENTION_FACTOR = 0.20
    # Pay special attention to cases where somebody is said to be "ex" something,
    # i.e. "fyrrverandi"
    EX_MENTION_FACTOR = 0.35

    # Sort the keys by decreasing mention weight
    rl = sorted(rd.keys(), key=lambda x: mention_weights[x], reverse=True)
    len_rl = len(rl)

    def is_ex(s):
        """ Does the given result contain an 'ex' prefix? """
        return any(
            contained(x, s)
            for x in ("fyrrverandi", "fráfarandi", "áður", "þáverandi", "fyrrum")
        )

    # Do a comparison of all pairs in the result list
    for i in range(len_rl - 1):
        ri = rl[i]
        crosses = 0
        ex_i = is_ex(ri)
        for j in range(i + 1, len_rl):
            rj = rl[j]
            if contained(rj, ri) or contained(ri, rj):
                crosses += 1
                # Result rj contains ri or vice versa:
                # Cross-add a part of the respective mention weights
                ex_j = is_ex(rj)
                if ex_i and not ex_j:
                    # We already had "fyrrverandi forseti Íslands" and now we
                    # get "forseti Íslands": reinforce "fyrrverandi forseti Íslands"
                    scores[ri] += mention_weights[rj] * EX_MENTION_FACTOR
                else:
                    scores[rj] += mention_weights[ri] * CROSS_MENTION_FACTOR / crosses
                if ex_j and not ex_i:
                    # We already had "forseti Íslands" and now we
                    # get "fyrrverandi forseti Íslands":
                    # reinforce "fyrrverandi forseti Íslands"
                    scores[rj] += mention_weights[ri] * EX_MENTION_FACTOR
                else:
                    scores[ri] += mention_weights[rj] * CROSS_MENTION_FACTOR / crosses
                if crosses == _MAX_MENTIONS:
                    # Don't bother with more than 5 cross mentions
                    break

    # Sort by decreasing score
    rl = sorted(
        [(s, sort_articles(articles)) for s, articles in rd.items()],
        key=lambda x: scores[x[0]],
        reverse=True,
    )

    # If we have 5 or more titles/definitions with more than one associated URL,
    # cut off those that have only one source URL
    if len(rl) > _CUTOFF_AFTER and len(rl[_CUTOFF_AFTER][1]) > 1:
        rl = [val for val in rl if len(val[1]) > 1]

    # Crop the article url lists down to _MAX_URLS
    return [dict(answer=a[0], sources=a[1][0:_MAX_URLS]) for a in rl[0:_MAXLEN_ANSWER]]


def prepare_response(q, prop_func):
    """ Prepare and return a simple (one-query) response """
    rd = defaultdict(dict)
    append_answers(rd, q, prop_func)
    return make_response_list(rd)


def add_entity_to_register(name, register, session, all_names=False):
    """ Add the entity name and the 'best' definition to the given
        name register dictionary. If all_names is True, we add
        all names that occur even if no title is found. """
    if name in register:
        # Already have a definition for this name
        return
    if " " not in name:
        # Single name: this might be the last name of a person/entity
        # that has already been mentioned by full name
        for k in register.keys():
            parts = k.split()
            if len(parts) > 1 and parts[-1] == name:
                # Reference to the last part of a previously defined
                # multi-part person or entity name,
                # for instance 'Clinton' -> 'Hillary Rodham Clinton'
                register[name] = dict(kind="ref", fullname=k)
                return
        # Not found as-is, but the name ends with an 's':
        # Check again for a possessive version, i.e.
        # 'Steinmeiers' referring to 'Steinmeier',
        # or 'Clintons' referring to 'Clinton'
        if name[-1] == "s":
            name_nominative = name[0:-1]
            for k in register.keys():
                parts = k.split()
                if len(parts) > 1 and parts[-1] == name_nominative:
                    register[name] = dict(kind="ref", fullname=k)
                    return
    # Use the query module to return definitions for an entity
    definition = query_entity_def(session, name)
    if definition:
        register[name] = dict(kind="entity", title=definition)
    elif all_names:
        register[name] = dict(kind="entity", title=None)


def add_name_to_register(name, register, session, all_names=False):
    """ Add the name and the 'best' title to the given name register dictionary """
    if name in register:
        # Already have a title for this exact name; don't bother
        return
    # Use the query module to return titles for a person
    title = query_person_title(session, name)
    name_key = name_key_to_update(register, name)
    if name_key is not None:
        if title:
            register[name_key] = dict(kind="name", title=title)
        elif all_names:
            register[name_key] = dict(kind="name", title=None)


def create_name_register(tokens, session, all_names=False):
    """ Assemble a dictionary of person and entity names
        occurring in the token list """
    register = {}
    for t in tokens:
        if t.kind == TOK.PERSON:
            gn = t.val
            for pn in gn:
                add_name_to_register(pn.name, register, session, all_names=all_names)
        elif t.kind == TOK.ENTITY:
            add_entity_to_register(t.txt, register, session, all_names=all_names)
    return register


def _query_person_titles(session, name):
    """ Return a list of all titles for a person """
    rd = defaultdict(dict)
    q = (
        session.query(
            Person.title,
            Article.id,
            Article.timestamp,
            Article.heading,
            Root.domain,
            Article.url,
        )
        .filter(Person.name == name)
        .filter(Root.visible == True)
        .join(Article, Article.url == Person.article_url)
        .join(Root)
        .order_by(Article.timestamp)
        .all()
    )
    # Append titles from the persons table
    append_answers(rd, q, prop_func=lambda x: x.title)
    # Also append definitions from the entities table, if any
    q = (
        session.query(
            Entity.definition,
            Article.id,
            Article.timestamp,
            Article.heading,
            Root.domain,
            Article.url,
        )
        .filter(Entity.name == name)
        .filter(Root.visible == True)
        .join(Article, Article.url == Entity.article_url)
        .join(Root)
        .order_by(Article.timestamp)
        .all()
    )
    append_answers(rd, q, prop_func=lambda x: x.definition)
    return make_response_list(rd)


def _query_article_list(session, name):
    """ Return a list of dicts with information about articles
        where the given name appears """
    articles = ArticleListQuery.articles(
        name, limit=_MAXLEN_ANSWER, enclosing_session=session
    )
    # Each entry is uuid, heading, timestamp (as ISO format string), domain
    # Collapse identical headings and remove empty ones
    adict = {
        a[1]: dict(
            uuid=str(a[0]),
            heading=a[1],
            ts=a[2].isoformat()[0:16],
            domain=a[3],
            url=a[4],
        )
        for a in articles
        if a[1]
    }
    return sorted(adict.values(), key=lambda x: x["ts"], reverse=True)


def query_person(query, session, name):
    """ A query for a person by name """
    titles = _query_person_titles(session, name)
    # Now, create a list of articles where this person name appears
    articles = _query_article_list(session, name)
    response = dict(answers=titles, sources=articles)
    if titles and "answer" in titles[0]:
        # 'Már Guðmundsson er seðlabankastjóri.'
        voice_answer = name + " er " + titles[0]["answer"] + "."
    else:
        voice_answer = "Ég veit ekki hver " + name + " er."
    return response, voice_answer


def query_person_title(session, name):
    """ Return the most likely title for a person """
    rl = _query_person_titles(session, name)
    return correct_spaces(rl[0]["answer"]) if rl else ""


def query_title(query, session, title):
    """ A query for a person by title """
    # !!! Consider doing a LIKE '%title%', not just LIKE 'title%'
    rd = defaultdict(dict)
    title_lc = title.lower()  # Query by lowercase title
    q = (
        session.query(
            Person.name,
            Article.id,
            Article.timestamp,
            Article.heading,
            Root.domain,
            Article.url,
        )
        .filter(Person.title_lc.like(title_lc + " %") | (Person.title_lc == title_lc))
        .filter(Root.visible == True)
        .join(Article, Article.url == Person.article_url)
        .join(Root)
        .order_by(Article.timestamp)
        .all()
    )
    # Append names from the persons table
    append_names(rd, q, prop_func=lambda x: x.name)
    # Also append definitions from the entities table, if any
    q = (
        session.query(
            Entity.name,
            Article.id,
            Article.timestamp,
            Article.heading,
            Root.domain,
            Article.url,
        )
        .filter(Entity.definition == title)
        .filter(Root.visible == True)
        .join(Article, Article.url == Entity.article_url)
        .join(Root)
        .order_by(Article.timestamp)
        .all()
    )
    append_names(rd, q, prop_func=lambda x: x.name)
    response = make_response_list(rd)
    if response and title and "answer" in response[0]:
        # Return 'Seðlabankastjóri er Már Guðmundsson.'
        upper_title = title[0].upper() + title[1:]
        voice_answer = upper_title + " er " + response[0]["answer"] + "."
    else:
        voice_answer = "Ég veit ekki hver er " + title + "."
    return response, voice_answer


def _query_entity_definitions(session, name):
    """ A query for definitions of an entity by name """
    q = (
        session.query(
            Entity.verb,
            Entity.definition,
            Article.id,
            Article.timestamp,
            Article.heading,
            Root.domain,
            Article.url,
        )
        .filter(Entity.name == name)
        .filter(Root.visible == True)
        .join(Article, Article.url == Entity.article_url)
        .join(Root)
        .order_by(Article.timestamp)
        .all()
    )
    return prepare_response(q, prop_func=lambda x: x.definition)


def query_entity(query, session, name):
    """ A query for an entity by name """
    titles = _query_entity_definitions(session, name)
    articles = _query_article_list(session, name)
    response = dict(answers=titles, sources=articles)
    if titles and "answer" in titles[0]:
        # 'Mál og menning er bókmenntafélag.'
        voice_answer = name + " er " + titles[0]["answer"] + "."
    else:
        voice_answer = "Ég veit ekki hvað " + name + " er."
    return response, voice_answer


def query_entity_def(session, name):
    """ Return a single (best) definition of an entity """
    rl = _query_entity_definitions(session, name)
    return correct_spaces(rl[0]["answer"]) if rl else ""


def query_company(query, session, name):
    """ A query for an company in the entities table """
    # Create a query name by cutting off periods at the end
    # (hf. -> hf) and adding a percent pattern match at the end
    qname = name.strip()
    while qname and qname[-1] == ".":
        qname = qname[:-1]
    q = (
        session.query(
            Entity.verb,
            Entity.definition,
            Article.id,
            Article.timestamp,
            Article.heading,
            Root.domain,
            Article.url,
        )
        .filter(Root.visible == True)
        .join(Article, Article.url == Entity.article_url)
        .join(Root)
        .order_by(Article.timestamp)
    )
    q = q.filter(Entity.name.like(qname + "%"))
    q = q.all()
    response = prepare_response(q, prop_func=lambda x: x.definition)
    if response and response[0]["answer"]:
        voice_answer = name + " er " + response[0]["answer"] + "."
    else:
        voice_answer = "Ég veit ekki hvað " + name + " er."
    return response, voice_answer


def query_word(query, session, stem):
    """ A query for words related to the given stem """
    # Count the articles where the stem occurs
    acnt = ArticleCountQuery.count(stem, enclosing_session=session)
    rlist = RelatedWordsQuery.rel(stem, enclosing_session=session) if acnt else []
    # Convert to an easily serializable dict
    # Exclude the original search stem from the result
    return dict(
        count=acnt,
        answers=[
            dict(stem=rstem, cat=rcat) for rstem, rcat, rcnt in rlist if rstem != stem
        ],
    )


def launch_search(query, session, qkey):
    """ Launch a search with the given search terms """
    pgs, stats = TreeUtility.raw_tag_toklist(
        session, query.token_list(), root=_QUERY_ROOT
    )

    # Collect the list of search terms
    terms = []
    tweights = []
    fixups = []
    for pg in pgs:
        for sent in pg:
            for t in sent:
                # Obtain search stems for the tokens.
                d = dict(x=t["x"], w=0.0)
                tweights.append(d)
                # The terms are represented as (stem, category) tuples.
                stems = stems_of_token(t)
                if stems:
                    terms.extend(stems)
                    fixups.append((d, len(stems)))

    assert sum(n for _, n in fixups) == len(terms)

    if Settings.DEBUG:
        print("Terms are:\n   {0}".format(terms))

    # Launch the search and return the answers, as well as the
    # search terms augmented with information about
    # whether and how they were used
    result = Search.list_similar_to_terms(session, terms, _MAXLEN_SEARCH)

    if "weights" not in result or not result["weights"]:
        # Probably unable to connect to the similarity server
        raise RuntimeError("Unable to connect to the similarity server")

    weights = result["weights"]
    assert len(weights) == len(terms)
    # Insert the weights at the proper places in the
    # token weight list
    index = 0
    for d, n in fixups:
        d["w"] = sum(weights[index : index + n]) / n
        index += n
    return dict(answers=result["articles"], weights=tweights)


_QFUNC = {
    "Person": query_person,
    "Title": query_title,
    "Entity": query_entity,
    "Company": query_company,
    "Word": query_word,
    "Search": launch_search,
}


def sentence(state, result):
    """ Called when sentence processing is complete """
    q = state["query"]
    if "qtype" in result:
        # Successfully matched a query type
        q.set_qtype(result.qtype)
        q.set_key(result.qkey)
        session = state["session"]
        # Select a query function and exceute it
        qfunc = _QFUNC.get(result.qtype)
        if qfunc is None:
            q.set_answer(result.qtype + ": " + result.qkey)
        else:
            try:
                voice_answer = None
                answer = qfunc(q, session, result.qkey)
                if isinstance(answer, tuple):
                    # We have both a normal and a voice answer
                    answer, voice_answer = answer
                q.set_answer(answer, voice_answer)
            except AssertionError:
                raise
            except Exception as e:
                q.set_error("E_EXCEPTION: {0}".format(e))
    else:
        q.set_error("E_QUERY_NOT_UNDERSTOOD")


GRAMMAR = """

# ----------------------------------------------
#
# Query grammar
#
# The following grammar is used for queries only
#
# ----------------------------------------------

Queries →
    QPerson > QCompany > QEntity > QTitle > QWord > QSearch

QPerson →
    Manneskja_nf
    | QPersonPrefix_nf Manneskja_nf '?'?
    | QPersonPrefix_þf Manneskja_þf '?'?
    | QPersonPrefix_þgf Manneskja_þgf # '?'?
    | QPersonPrefix_ef Manneskja_ef '?'?
    | QPersonPrefixAny Sérnafn '?'?

QPersonPrefixAny →
    QPersonPrefix/fall

$score(-2) QPersonPrefixAny # Discourage entity names if person names are available

QPersonPrefix_nf →
    "hver" "er"
    | "hvað" "gerir"
    | "hvað" "starfar"
    | "hvaða" "titil" "hefur"
    | "hvaða" "starfi" "gegnir"

QPersonPrefix_þf →
    "hvað" "veistu" "um"
    | "hvað" "geturðu" "sagt" "mér"? "um"

QPersonPrefix_ef →
    "hver" "er" "titill"
    | "hver" "er" "starfstitill"
    | "hvert" "er" "starf"

QPersonPrefix_þgf →
    "segðu" "mér"? "frá"

QCompany →
    # Það þarf að gera ráð fyrir sérstökum punkti í
    # enda fyrirspurnarinnar þar sem punktur á eftir 'hf.'
    # eða 'ehf.' í enda setningar er skilinn frá
    # skammstöfunar-tókanum.
    QCompanyPrefix_nf Fyrirtæki_nf '.'? '?'?
    | QCompanyPrefix_þf Fyrirtæki_þf '.'? '?'?
    | QCompanyPrefix_þgf Fyrirtæki_þgf '.' # '?'?

QCompanyPrefix_nf →
    "hvað" "er"

QCompanyPrefix_þf →
    "hvað" "veistu" "um"
    | "hvað" "geturðu" "sagt" "mér"? "um"

QCompanyPrefix_þgf →
    "segðu" "mér"? "frá"

QEntity → QEntityPrefix/fall QEntityKey/fall '.'? '?'?

QEntityKey/fall →
    Sérnafn/fall > Sérnafn > no/fall > no

QEntityPrefix_nf →
    "hvað" "er"
    | "hvað" "eru"
    | 0

QEntityPrefix_þf →
    "hvað" "veistu" "um"
    | "hvað" "geturðu" "sagt" "mér"? "um"

QEntityPrefix_þgf →
    "segðu" "mér"? "frá"

QEntityPrefix_ef →
    0

QTitle →
    QTitlePrefix_nf QTitleKey_nf '?'?
    | QTitlePrefix_ef QTitleKey_ef '?'?

QSegðuMér →
    "segðu" "mér"
    | "mig" "langar" "að" "vita"
    | "ég" "vil" "gjarnan"? "vita"

QTitlePrefix_nf →
    QSegðuMér? QTitlePrefixFrh_nf

QTitlePrefixFrh_nf →
    "hver" "er"
    | "hver" "var"
    | "hver" "hefur" "verið"

QTitlePrefix_ef →
    QSegðuMér? "hver" "gegnir" "starfi"

QTitleKey_nf →
    EinnTitill_nf OgTitill_nf*

QTitleKey_ef →
    EinnTitill_ef OgTitill_ef*

# Word relation query

QWord →
    QWordPerson
    > QWordEntity
    > QWordNoun
    > QWordVerb

QWordPrefix_þgf →
    "hvað" "tengist"
    | "hvað" "er" "tengt"
    | "hvaða" "orð" "tengjast"
    | "hvaða" "orð" "tengist"
    | "hvaða" "orð" "eru" "tengd"

QWordPrefix_nf →
    "hverju" "tengist"
    | "hvaða" "orðum" "tengist"

# 'Hvað tengist [orðinu/nafnorðinu] útihátíð?'

QWordNoun →
    QWordNoun_nf
    | QWordNoun_þgf

QWordNoun_þgf →
    QWordPrefix_þgf QWordNounKey_þgf '?'?

QWordNoun_nf →
    QWordNounKey_nf
    | QWordPrefix_þgf "orðinu" QWordNounKey_nf '?'?
    | QWordPrefix_þgf "nafnorðinu" QWordNounKey_nf '?'?
    | QWordPrefix_nf "orðið" QWordNounKey_nf '?'?
    | QWordPrefix_nf "nafnorðið" QWordNounKey_nf '?'?

QWordNounKey/fall → no/fall

# 'Hvaða orð tengjast Ragnheiði Ríkharðsdóttur?'
# 'Hvaða orð eru tengd nafninu Elliði Vignisson?'

QWordPerson →
    QWordPerson_nf
    | QWordPerson_þgf

QWordPerson_þgf →
    QWordPrefix_þgf QWordPersonKey_þgf '?'?

QWordPerson_nf →
    QWordPrefix_þgf "nafninu" QWordPersonKey_nf '?'?
    | QWordPrefix_nf "nafnið" QWordPersonKey_nf '?'?

QWordPersonKey/fall → person/fall

# 'Hvaða orð tengjast sögninni að teikna?'

QWordVerb →
    Nhm? QWordVerbKey
    | QWordPrefix_þgf "orðinu" QWordVerbKey '?'?
    | QWordPrefix_þgf "sögninni" Nhm? QWordVerbKey '?'?
    | QWordPrefix_þgf "sagnorðinu" Nhm? QWordVerbKey '?'?
    | QWordPrefix_nf "orðið" QWordVerbKey '?'?
    | QWordPrefix_nf "sögnin" Nhm? QWordVerbKey '?'?
    | QWordPrefix_nf "sagnorðið" Nhm? QWordVerbKey '?'?

QWordVerbKey → so_nh

# 'Hvaða orð tengjast Wintris?'

QWordEntity →
    QWordEntityKey_nf
    | QWordPrefix_þgf QWordEntityKey_þgf '?'?
    | QWordPrefix_þgf "orðinu" QWordEntityKey_nf '?'?
    | QWordPrefix_þgf "nafninu" QWordEntityKey_nf '?'?
    | QWordPrefix_þgf "sérnafninu" QWordEntityKey_nf '?'?
    | QWordPrefix_þgf "heitinu" QWordEntityKey_nf '?'?
    | QWordPrefix_nf "orðið" QWordEntityKey_nf '?'?
    | QWordPrefix_nf "nafnið" QWordEntityKey_nf '?'?
    | QWordPrefix_nf "sérnafnið" QWordEntityKey_nf '?'?
    | QWordPrefix_nf "heitið" QWordEntityKey_nf '?'?

QWordEntityKey/fall → Sérnafn/fall > Sérnafn

# Arbitrary search

# Try to recognize the search query first as a sentence,
# then as a noun phrase, and finally as an arbitrary sequence of tokens

QSearch →
    QSearchSentence
    | QSearchNl
    | QSearchArbitrary

QSearchSentence →
    Málsgrein
    | SetningÁnF_et_p3/kyn Lokatákn? # 'Stefndi í átt til Bláfjalla'
    | SetningÁnF_ft_p3/kyn Lokatákn? # 'Aftengdu fjarstýringu'

$score(+4) QSearchSentence

QSearchNl →
    Nl_nf Atviksliður? Lokatákn? # 'Tobías í turninum'

QSearchArbitrary →
    QSearchToken+ Lokatákn?

$score(-100) QSearchArbitrary

QSearchToken →
    person/fall/kyn > fyrirtæki > no/fall/tala/kyn
    > fn/fall/tala/kyn > pfn/fall/tala/kyn > entity > lo > so
    > eo > ao > fs/fall
    > dags > dagsafs > dagsföst
    > tímapunkturafs > tímapunkturfast > tími
    > raðnr > to > töl > ártal > tala > sérnafn

"""


# The following functions correspond to grammar nonterminals (see Reynir.grammar)
# and are called during tree processing (depth-first, i.e. bottom-up navigation)


def QPerson(node, params, result):
    """ Person query """
    result.qtype = "Person"
    if "mannsnafn" in result:
        result.qkey = result.mannsnafn
    elif "sérnafn" in result:
        result.qkey = result.sérnafn
    else:
        assert False


def QCompany(node, params, result):
    result.qtype = "Company"
    result.qkey = result.fyrirtæki


def QEntity(node, params, result):
    result.qtype = "Entity"
    assert "qkey" in result


def QTitle(node, params, result):
    result.qtype = "Title"
    result.qkey = result.titill


def QWord(node, params, result):
    result.qtype = "Word"
    assert "qkey" in result


def QSearch(node, params, result):
    result.qtype = "Search"
    # Return the entire query text as the search key
    result.qkey = result._text


def Sérnafn(node, params, result):
    """ Sérnafn, stutt eða langt """
    result.sérnafn = result._nominative


def Fyrirtæki(node, params, result):
    """ Fyrirtækisnafn, þ.e. sérnafn + ehf./hf./Inc. o.s.frv. """
    result.fyrirtæki = result._nominative


def Mannsnafn(node, params, result):
    """ Hreint mannsnafn, þ.e. án ávarps og titils """
    result.mannsnafn = result._nominative


def EfLiður(node, params, result):
    """ Eignarfallsliðir haldast óbreyttir,
        þ.e. þeim á ekki að breyta í nefnifall """
    result._nominative = result._text


def FsMeðFallstjórn(node, params, result):
    """ Forsetningarliðir haldast óbreyttir,
        þ.e. þeim á ekki að breyta í nefnifall """
    result._nominative = result._text


def QEntityKey(node, params, result):
    if "sérnafn" in result:
        result.qkey = result.sérnafn
    else:
        result.qkey = result._nominative


def QTitleKey(node, params, result):
    """ Titill """
    result.titill = result._nominative


def QWordNounKey(node, params, result):
    result.qkey = result._canonical


def QWordPersonKey(node, params, result):
    if "mannsnafn" in result:
        result.qkey = result.mannsnafn
    elif "sérnafn" in result:
        result.qkey = result.sérnafn
    else:
        result.qkey = result._nominative


def QWordEntityKey(node, params, result):
    result.qkey = result._nominative


def QWordVerbKey(node, params, result):
    result.qkey = result._root

