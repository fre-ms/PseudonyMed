# Pseudonyms: reproducible identifiers and error-tolerant matching

PseudonyMed replaces the main person of a document by `Patient <KENNUNG>` (or
`Patientin …`), relatives by `Angehörige <KENNUNG>-1`, `-2` … and referring
physicians by `Dr. Z1`, `Dr. Z2` … The identifier (*Kennung*) must stay the
same for the same person across documents, sessions and machines, and it must
tolerate the spelling variants that occur in practice (Müller/Mueller, a typo
in a first name, swapped first and last name, a slip in the date of birth).

The design borrows from the **Mainzelliste**, the open-source pseudonymisation
service of the Universitätsmedizin Mainz (Lablans, Borg & Ückert 2015), which
combines two independent mechanisms: an error-tolerant record linkage decides
whether a person is already known, and a generator mints an identifier for
new people. PseudonyMed keeps that separation and adapts both halves to a local,
single-practice setting.

## 1 Who is already known: record linkage

Before an identifier is issued, the patient is compared with everybody in the
local pseudonym store. The comparison follows the EpiLink algorithm
(Contiero, Tittarelli et al. 2005) as configured in the Mainzelliste:

| Field | Comparison | Weight |
|---|---|---|
| surname (incl. birth name) | Dice coefficient over character bigrams, best over name components, phonetic equality counts 0.9 | 15.2 |
| first name(s) | same | 12.0 |
| year of birth | equal / not equal | 5.1 |
| day of birth | equal / not equal, 0.5 when day and month are swapped | 4.9 |
| month of birth | same | 3.6 |

The weights are the EpiLink weights `w = log2((1 − errorRate) / frequency)`
computed from the Mainzelliste defaults (`config/mainzelliste.conf.default`:
first name frequency 0.000235, error rate 0.01; surname 0.0000271 / 0.008;
day 0.0333 / 0.005; month 0.0833 / 0.002; year 0.0286 / 0.004). Swapped first
and last names are tried as well (the Mainzelliste's *exchange group*), and
compound names are compared component-wise (its *GermanLastNameDecomposer*
and *FirstNameDecomposer*). The score is the weighted mean over the fields
present on both sides.

Decision rules, in this order:

* equal canonical identity (section 2: phonetically equal names and the same
  date of birth): the same person, because the hash scheme would mint the
  same code on an empty machine anyway; `--neu` overrides.
* score ≥ 0.95: the same person. The new spelling is stored as an alias of
  the existing entry and the existing identifier is reused. The report says
  so (`Patient dem vorhandenen Eintrag … zugeordnet`).
* 0.80 ≤ score < 0.95: uncertain. The command line lists the candidates and
  stops; decide with `--gleich KENNUNG` or `--neu`. (The Mainzelliste's lower
  threshold is 0.6; PseudonyMed uses 0.8 so that a mere namesake with a
  different date of birth does not ask every time.)
* score < 0.80: a different person.

Typical scores: a typo in the surname 0.97, a different year of birth 0.87,
day and month swapped 0.90, same name but entirely different date of birth
0.67. Twins with phonetically identical first names (Anna/Anne) and the same
date of birth would be merged automatically; the warning in the report is the
place to notice that, and `--neu` separates them.

## 2 Minting an identifier: keyed hash with check character

For a new person the identifier is derived from a **canonical identity**:

```
Kölner Phonetik(main part of surname) | Kölner Phonetik(first given name) | date of birth (ISO)
```

The Kölner Phonetik (Postel 1969) maps German names to digit codes that
ignore the usual spelling differences: Müller, Mueller and Miller are all
657, Meier, Maier, Mayer and Meyer are 67. "Main part" is the longest
component of a compound surname, particles such as *von* or *de* dropped.
Without a date of birth the normalised names are used instead, because
phonetic codes alone would merge different people.

The identity is hashed with **HMAC-SHA256 under a secret key** that is
created on first use and stored next to the pseudonym database
(`pseudonymschluessel.txt`, mode 0600; `pseudonymed konfig` shows the path).
The first 25 bits of the digest become five characters of a 32-symbol
alphabet without look-alikes (`ABCDEFGHJKLMNPQRSTUVWXYZ23456789`), and a
sixth character is a **Luhn mod 32 check character**. Example: `Patient K7Q4MX`.

Consequences:

* Same key, same person, same code, on every machine: copy the key file along
  with the database (or alone: the code can be recomputed without the
  database, the mapping back to clear data cannot).
* Without the key a code cannot be linked to a person by trying names: an
  attacker would have to guess the 256-bit key, not just enumerate names and
  dates of birth, which would be feasible against a plain hash.
* Spelling variants give the same code even on a fresh machine with an empty
  database, because the phonetic canonical form absorbs them.
* The check character catches every single-character error and almost every
  transposition of neighbouring characters, so a code the language model
  mangled is noticed by `pseudonymed rueckersetzen`, which also suggests the
  stored code one edit away.
* Two different people with the same canonical identity (same date of birth,
  phonetically identical names, declared different with `--neu`) get a
  variant code derived from the same identity with a counter.

The Mainzelliste's own identifier, the PID of Faldum & Pommerening (2005), is
constructed differently: an 8-character code over a 32-symbol alphabet that
encrypts a running counter with three secret keys and carries enough
redundancy to detect two errors and correct one error or one transposition.
It depends on the service's counter, which is exactly what PseudonyMed wants to
avoid, so PseudonyMed keeps the alphabet and the idea of a check character but
derives the code from the identity instead. The Mainzelliste can also link
records on Bloom-filter encodings of the names (Schnell, Bachteler & Reiher
2009) so that several sites can match without exchanging clear names; this is
not needed for a single local store and is not implemented.

The previous scheme, a running letter (`Patient A`, `B`, …), remains
available with `--schema letter`; existing entries keep their identifier
whatever the scheme.

## 3 Streets and towns

The same chain gives every street and every town its own reproducible code,
so that a letter can tell the patient's home from the referrer's practice and
the re-identification can put each back exactly:

| In the document | Pseudonym | Canonical identity hashed |
|---|---|---|
| Musterstraße 12 / Musterstr. 12 / Muster-Strasse 12 | `[Straße K7Q4MX]` | `strasse:` Kölner Phonetik of the name with the *straße/strasse/str.* suffix normalised `:str:` house number (`12`, `12a`) |
| 80331 München / D-80331 Muenchen | `[PLZ Ort 2F7QV4]` | `ort:` Kölner Phonetik of the town, particles (*am*, *an der*, *Bad*, *OT*) ignored |
| München / MÜNCHEN (bare) | `[Ort 2F7QV4]` | the same, hence the same code |

Other suffixes (*Lindenweg*, *Marktplatz*) stay inside the phonetic code, so
*Lindenweg 5* and *Lindenstraße 5* differ; words run together before encoding,
so *Linden-Weg* equals *Lindenweg*. The house number is part of the street
identity: the same street with another number is another code, which is what
you want when two people of one household appear in a document. The postcode
is deliberately not part of the town identity, otherwise a bare "München" in
the running text could not share the code of "80331 München" in the address
block; the two placeholders keep the postcode recoverable. Relatives' and
referring physicians' addresses get their own codes (`[Straße …]` of the
practice instead of the former `[Praxisanschrift]`).

Codes need the same secret key as the patient identifiers; without a key
(for example in unit tests without a store) the placeholders fall back to
`[Wohnort]`.

## References

* Lablans M, Borg A, Ückert F. A RESTful interface to pseudonymization services in modern web applications. *BMC Medical Informatics and Decision Making* 2015;15:2. doi:10.1186/s12911-014-0123-5
* Faldum A, Pommerening K. An optimal code for patient identifiers. *Computer Methods and Programs in Biomedicine* 2005;79(1):81–88. doi:10.1016/j.cmpb.2005.03.004
* Contiero P, Tittarelli A, Tagliabue G, Maghini A, Fabiano S, Crosignani P, Tessandori R. The EpiLink record linkage software: presentation and results of linkage test on cancer registry files. *Methods of Information in Medicine* 2005;44(1):66–71. doi:10.1055/s-0038-1633924
* Schnell R, Bachteler T, Reiher J. Privacy-preserving record linkage using Bloom filters. *BMC Medical Informatics and Decision Making* 2009;9:41. doi:10.1186/1472-6947-9-41
* Postel HJ. Die Kölner Phonetik. Ein Verfahren zur Identifizierung von Personennamen auf der Grundlage der Gestaltanalyse. *IBM-Nachrichten* 1969;19:925–931.
* Mainzelliste source and default configuration: https://bitbucket.org/medicalinformatics/mainzelliste (config/mainzelliste.conf.default, read 30 October 2026).
