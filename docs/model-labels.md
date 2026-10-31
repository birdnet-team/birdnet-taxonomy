# Model Labels

How an audio model's outputs map onto the taxonomy. Contributor rules are in [CONTRIBUTING.md](../CONTRIBUTING.md#model-labels).

`overrides/model_labels/<model>.csv` holds one row per output of an audio model
and maps it onto the taxonomy, so a model can keep its own labels:

| Column | Meaning |
|--------|---------|
| `mario_id` | The model's label ID |
| `model_label` | The label as it appears in the model |
| `train_count` | Training samples |
| `status` | `keep`, `pending` (its record is not added yet), `remove`, or `review` |
| `scientific_name` | Taxonomy record the label belongs to |
| `taxon_group` | Group, used until the record exists |
| `redirect_to` | Label this one merges into, if any |
| `alias` | Name to add as an alias |
| `reason` | Code for why the label is flagged or redirected; required on `remove` and `review` rows |
| `note` | Optional specifics that replace the code's standard wording |

`redirect_to` says which class a label should end up in, and `status` says when:
`remove` merges it now, `keep` leaves it an output until a future model. The target
must be a surviving label with training samples that does not redirect itself. Each
`alias` goes on the record of the redirect target, or on the label's own record, and
stays one-way. A record that redirects keeps winning lookups of its own name, so a
common-name alias may match its name.

The build fails on an invalid crosswalk, then writes `dist/model_labels/<model>.csv`
for the model developer, with resolved BirdNET IDs. Its `reason_text` column states in
plain language why each label was rejected or flagged, and `rename_to` proposes a name
for each surviving label whose name differs from its record. Reasons and proposed
renames go only to the developer: they are not on taxonomy records, in the release
zip, or in the API.
