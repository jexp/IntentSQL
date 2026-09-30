"""User-supplied CS50 SQL exercise prompts and SQLite answer oracles.

These are benchmark fixtures, never planner rules. The database itself supplies
the exact answer. `expected_rows` is an independent dimensional check.
"""

CASES = [
    # Cyberchase: one relation, filters, missing values, ordering, aggregation.
    dict(id="C1", database="cyberchase.db", expected_rows=26,
         question="Show me just the titles from the original season of Cyberchase.",
         gold_sql="SELECT title FROM episodes WHERE season = 1"),
    dict(id="C2", database="cyberchase.db", expected_rows=14,
         question="For each season, what was its first episode? Give me the season and title.",
         gold_sql="SELECT season, title FROM episodes WHERE episode_in_season = 1"),
    dict(id="C3", database="cyberchase.db", expected_rows=1,
         question="What's the PBS production code for Hackerized!?",
         gold_sql="SELECT production_code FROM episodes WHERE title = 'Hackerized!'",
         paraphrases=[
             "What's Hackerized!'s production code?",
             "Find Hackerized! and tell me its PBS code.",
             "I need the internal code corresponding to the episode named Hackerized!.",
             "Which production identifier belongs to Hackerized!?",
             "Can you look up Hackerized! for me? Just its production code.",
         ]),
    dict(id="C4", database="cyberchase.db", expected_rows=26,
         question="Which episodes don't have a topic assigned yet? Just give me their titles.",
         gold_sql="SELECT title FROM episodes WHERE topic IS NULL"),
    dict(id="C5", database="cyberchase.db", expected_rows=1,
         question="What episode aired on December 31, 2004?",
         gold_sql="SELECT title FROM episodes WHERE air_date = '2004-12-31'"),
    dict(id="C6", database="cyberchase.db", expected_rows=2,
         question="I only want the titles of season 6 episodes that actually came out during 2007.",
         gold_sql="SELECT title FROM episodes WHERE season = 6 AND air_date BETWEEN '2007-01-01' AND '2007-12-31'"),
    dict(id="C7", database="cyberchase.db", expected_rows=6,
         question="Show me the title and topic of every episode that teaches something about fractions.",
         gold_sql="SELECT title, topic FROM episodes WHERE topic LIKE '%fractions%'",
         paraphrases=[
             "Show me everything about episodes whose lessons involve fractions.",
             "Which episodes deal with fractions? I need their names and topics.",
             "I'm only interested in fraction-related episodes. Give me title plus topic.",
             "Can you pull the episode names together with their topics wherever fractions are being taught?",
             "Forget the other subjects; what Cyberchase episodes cover fractions?",
         ]),
    dict(id="C8", database="cyberchase.db", expected_rows=1,
         question="How many episodes came out from 2018 through 2023, including both years?",
         gold_sql="SELECT COUNT(*) FROM episodes WHERE air_date BETWEEN '2018-01-01' AND '2023-12-31'"),
    dict(id="C9", database="cyberchase.db", expected_rows=1,
         question="How many episodes were released during the first six years, 2002 through 2007?",
         gold_sql="SELECT COUNT(*) FROM episodes WHERE air_date BETWEEN '2002-01-01' AND '2007-12-31'"),
    dict(id="C10", database="cyberchase.db", expected_rows=140, ordered=True,
         question="Give me each episode's id, title and production code, sorted from the earliest production code onward.",
         gold_sql="SELECT id, title, production_code FROM episodes ORDER BY production_code ASC"),
    dict(id="C11", database="cyberchase.db", expected_rows=10, ordered=True,
         question="I only care about season 5. Give me its episode titles backwards alphabetically.",
         gold_sql="SELECT title FROM episodes WHERE season = 5 ORDER BY title DESC"),
    dict(id="C12", database="cyberchase.db", expected_rows=1,
         question="How many different episode titles are there?",
         gold_sql="SELECT COUNT(DISTINCT title) FROM episodes"),

    # DESE: joins, grouped restrictions, correlated and scalar subqueries.
    dict(id="D1", database="dese.db", expected_rows=1761,
         question="Give me the names and cities of Massachusetts public schools, but don't include charter schools.",
         gold_sql="SELECT name, city FROM schools WHERE type = 'Public School'"),
    dict(id="D2", database="dese.db", expected_rows=121,
         question="Which districts aren't operating anymore?",
         gold_sql="SELECT name FROM districts WHERE name LIKE '%(non-op)'"),
    dict(id="D3", database="dese.db", expected_rows=1,
         question="What's the average amount districts spend per pupil?",
         gold_sql="SELECT AVG(per_pupil_expenditure) FROM expenditures"),
    dict(id="D4", database="dese.db", expected_rows=10, ordered=True,
         question="Which 10 cities have the most public schools? Show the city and count, biggest first, alphabetically for ties.",
         gold_sql="SELECT city, COUNT(*) FROM schools WHERE type='Public School' GROUP BY city ORDER BY COUNT(*) DESC, city ASC LIMIT 10"),
    dict(id="D5", database="dese.db", expected_rows=201,
         question="Show cities that have no more than three public schools. Give me city and number of schools.",
         gold_sql="SELECT city, COUNT(*) FROM schools WHERE type='Public School' GROUP BY city HAVING COUNT(*) <= 3 ORDER BY COUNT(*) DESC, city ASC"),
    dict(id="D6", database="dese.db", expected_rows=9,
         question="Which schools had a perfect graduation rate?",
         gold_sql="SELECT schools.name FROM schools JOIN graduation_rates ON schools.id = graduation_rates.school_id WHERE graduation_rates.graduated = 100"),
    dict(id="D7", database="dese.db", expected_rows=17,
         question="What schools belong to the district called Cambridge?",
         gold_sql="SELECT schools.name FROM schools JOIN districts ON schools.district_id = districts.id WHERE districts.name = 'Cambridge'"),
    dict(id="D8", database="dese.db", expected_rows=396,
         question="Give me every school district together with its pupil count.",
         gold_sql="SELECT districts.name, expenditures.pupils FROM districts JOIN expenditures ON districts.id = expenditures.district_id"),
    dict(id="D9", database="dese.db", expected_rows=1,
         question="Which district has the smallest number of pupils? Preserve ties.",
         gold_sql="SELECT districts.name FROM districts JOIN expenditures ON districts.id=expenditures.district_id WHERE expenditures.pupils = (SELECT MIN(pupils) FROM expenditures)"),
    dict(id="D10", database="dese.db", expected_rows=10, ordered=True,
         question="What are the ten public school districts that spend the most per student?",
         gold_sql="SELECT districts.name, expenditures.per_pupil_expenditure FROM districts JOIN expenditures ON districts.id=expenditures.district_id WHERE districts.type='Public School District' ORDER BY expenditures.per_pupil_expenditure DESC LIMIT 10"),
    dict(id="D11", database="dese.db", expected_rows=391, ordered=True,
         question="Show every school with its graduation rate and the amount its district spends per pupil. Put the highest-spending schools first, and break spending ties alphabetically by school.",
         gold_sql="""SELECT schools.name, expenditures.per_pupil_expenditure, graduation_rates.graduated
                     FROM schools JOIN districts ON schools.district_id = districts.id
                     JOIN expenditures ON districts.id = expenditures.district_id
                     JOIN graduation_rates ON schools.id = graduation_rates.school_id
                     ORDER BY expenditures.per_pupil_expenditure DESC, schools.name ASC"""),
    dict(id="D12", database="dese.db", expected_rows=65, ordered=True,
         question="Find public school districts that spend more per pupil than the statewide district average and also have a higher-than-average percentage of exemplary teachers. Show district, expenditure and exemplary percentage. Rank exemplary percentage highest first, then expenditure highest first.",
         gold_sql="""SELECT districts.name, expenditures.per_pupil_expenditure, staff_evaluations.exemplary
                     FROM districts JOIN expenditures ON districts.id = expenditures.district_id
                     JOIN staff_evaluations ON districts.id = staff_evaluations.district_id
                     WHERE districts.type = 'Public School District'
                     AND expenditures.per_pupil_expenditure > (SELECT AVG(per_pupil_expenditure) FROM expenditures)
                     AND staff_evaluations.exemplary > (SELECT AVG(exemplary) FROM staff_evaluations)
                     ORDER BY staff_evaluations.exemplary DESC, expenditures.per_pupil_expenditure DESC"""),

    # Moneyball: composite joins, derived expressions and rankings.
    dict(id="M1", database="moneyball.db", expected_rows=17, ordered=True,
         question="How did the average MLB salary change over time? Give me year and average salary rounded to two decimals, newest year first.",
         gold_sql="SELECT year, ROUND(AVG(salary), 2) FROM salaries GROUP BY year ORDER BY year DESC"),
    dict(id="M2", database="moneyball.db", expected_rows=17, ordered=True,
         question="Show me Cal Ripken's salary history, newest year first.",
         gold_sql="""SELECT salaries.year, salaries.salary FROM players JOIN salaries ON players.id = salaries.player_id
                     WHERE players.first_name = 'Cal' AND players.last_name = 'Ripken' ORDER BY salaries.year DESC"""),
    dict(id="M3", database="moneyball.db", expected_rows=13, ordered=True,
         question="Give me the yearly home-run history for the Ken Griffey who was born in 1969. Newest season first.",
         gold_sql="""SELECT performances.year, performances.HR FROM players JOIN performances ON players.id = performances.player_id
                     WHERE players.first_name = 'Ken' AND players.last_name = 'Griffey' AND players.birth_year = 1969
                     ORDER BY performances.year DESC"""),
    dict(id="M4", database="moneyball.db", expected_rows=50, ordered=True,
         question="Who were the 50 lowest-paid players in 2001? Show first name, last name and salary. Cheapest first. Resolve equal salaries by first name, last name, then player id.",
         gold_sql="""SELECT players.first_name, players.last_name, salaries.salary FROM players
                     JOIN salaries ON players.id = salaries.player_id WHERE salaries.year = 2001
                     ORDER BY salaries.salary ASC, players.first_name ASC, players.last_name ASC, players.id ASC LIMIT 50"""),
    dict(id="M5", database="moneyball.db", expected_rows=3,
         question="Which teams did Satchel Paige play for? Don't repeat a team.",
         gold_sql="""SELECT DISTINCT teams.name FROM players JOIN performances ON players.id = performances.player_id
                     JOIN teams ON performances.team_id = teams.id WHERE players.first_name = 'Satchel' AND players.last_name = 'Paige'"""),
    dict(id="M6", database="moneyball.db", expected_rows=5, ordered=True,
         question="Which five teams produced the most total hits in 2001? Show the team and total hits, highest first.",
         gold_sql="""SELECT teams.name, SUM(performances.H) FROM teams JOIN performances ON teams.id = performances.team_id
                     WHERE performances.year = 2001 GROUP BY teams.id ORDER BY SUM(performances.H) DESC LIMIT 5"""),
    dict(id="M7", database="moneyball.db", expected_rows=1,
         question="Who received the highest salary in the entire database? Give me the player's first and last name.",
         gold_sql="""SELECT players.first_name, players.last_name FROM players JOIN salaries ON players.id = salaries.player_id
                     WHERE salaries.salary = (SELECT MAX(salary) FROM salaries)""",
         paraphrases=[
             "Who got paid more than anyone else ever?",
             "Who's the highest-paid player in this data?",
             "Find whoever received the biggest single salary.",
             "Which player has the all-time maximum salary?",
             "I just want the name of whoever got the largest paycheck.",
         ]),
    dict(id="M8", database="moneyball.db", expected_rows=1,
         question="What salary did the top home-run hitter of 2001 receive that year?",
         gold_sql="""SELECT salaries.salary FROM salaries JOIN performances
                     ON salaries.player_id = performances.player_id AND salaries.year = performances.year
                     WHERE performances.year = 2001 AND performances.HR =
                     (SELECT MAX(HR) FROM performances WHERE year = 2001)"""),
    dict(id="M9", database="moneyball.db", expected_rows=5, ordered=True,
         question="Which five teams had the lowest average salary in 2001? Give me team and average salary rounded to two decimals.",
         gold_sql="""SELECT teams.name, ROUND(AVG(salaries.salary), 2) FROM teams JOIN salaries
                     ON teams.id = salaries.team_id WHERE salaries.year = 2001 GROUP BY teams.id
                     ORDER BY ROUND(AVG(salaries.salary), 2) ASC LIMIT 5"""),
    dict(id="M10", database="moneyball.db", expected_rows=14915,
         question="For every player and year, show first name, last name, salary, home runs and year. Make sure salary and performance refer to the same year.",
         gold_sql="""SELECT players.first_name, players.last_name, salaries.salary, performances.HR, salaries.year
                     FROM players JOIN salaries ON players.id = salaries.player_id
                     JOIN performances ON players.id = performances.player_id AND salaries.year = performances.year
                     ORDER BY players.id ASC, salaries.year DESC, performances.HR DESC, salaries.salary DESC"""),
    dict(id="M11", database="moneyball.db", expected_rows=10, ordered=True,
         question="Who were the ten cheapest players per hit in 2001? Ignore players with zero hits. Show first name, last name and dollars per hit.",
         gold_sql="""SELECT players.first_name, players.last_name, salaries.salary / performances.H
                     FROM players JOIN salaries ON players.id = salaries.player_id
                     JOIN performances ON players.id = performances.player_id AND salaries.year = performances.year
                     WHERE salaries.year = 2001 AND performances.H > 0
                     ORDER BY salaries.salary / performances.H ASC, players.first_name ASC, players.last_name ASC LIMIT 10"""),
    dict(id="M12", database="moneyball.db", expected_rows=6,
         question="Which players are simultaneously among the ten cheapest per hit and the ten cheapest per RBI in 2001?",
         gold_sql="""WITH costs AS (
                         SELECT players.id, players.first_name, players.last_name,
                                salaries.salary * 1.0 / performances.H AS per_hit,
                                salaries.salary * 1.0 / performances.RBI AS per_rbi
                         FROM players JOIN salaries ON players.id = salaries.player_id
                         JOIN performances ON players.id = performances.player_id AND salaries.year = performances.year
                         WHERE salaries.year = 2001 AND performances.H > 0 AND performances.RBI > 0
                     ), hit_top AS (SELECT id FROM costs ORDER BY per_hit ASC LIMIT 10),
                     rbi_top AS (SELECT id FROM costs ORDER BY per_rbi ASC LIMIT 10)
                     SELECT DISTINCT costs.first_name, costs.last_name FROM costs
                     JOIN hit_top USING(id) JOIN rbi_top USING(id)"""),
]

# Frozen public-alpha regression envelope. These cases use capabilities the
# typed compiler currently claims. Exclusions are architectural, not selected
# after looking at model outcomes.
ALPHA_APPLICABLE_IDS = (
    *(f"C{index}" for index in range(1, 13)),
    *(f"D{index}" for index in range(1, 11)),
    "M1", "M7",
)

ALPHA_EXCLUSIONS = {
    "D11": "requires multiple joins",
    "D12": "requires multiple joins and scalar subqueries",
    "M2": "direct join with multiple source predicates",
    "M3": "direct join with multiple source predicates",
    "M4": "direct join with four ordering terms",
    "M5": "requires multiple joins",
    "M6": "aggregate over a join",
    "M8": "composite join plus a scalar subquery",
    "M9": "aggregate over a join",
    "M10": "multiple joins with a composite relationship",
    "M11": "derived expression over a join",
    "M12": "CTEs, subqueries, set intersection, and derived expressions",
}
