---
name: entering-labor-numbers
description: Use when the user wants to enter or change the labor numbers on a project's bidding labor panel.
metadata:
  requires_flags: [bidding]
  roles: [estimating_admin, executive, it_admin, estimating_engineer_materials, estimating_engineer_labor]
  version: "1"
---

# Entering labor numbers

"Labor numbers" means the bidding labor panel of one project: the labor amount, the labor notes, the verified flag and the labor breakdown lines. The tool is set_labor_numbers. If the user might mean something else (labor time, wage type, a labor note on the project details, a PM labor figure), ask which one before proposing anything.

## Steps

1. Find the project. Use the page context when it names one; otherwise call app_search with the number or name the user gave. If several projects match, list them with their numbers and ask which one.
2. Call get_labor first, with response_format detailed when the user talks about breakdown lines, so you know what is there now.
3. Ask for anything the user did not say. Never invent an amount, a note or a line.
4. Call set_labor_numbers once for the project. One card per project.

## Values

- Amounts are dollars as digits with up to two decimals: 12500 or 12500.50. Turn "12.5k" into 12500 only when the user clearly meant that; ask when unsure.
- Null leaves a field unchanged. Send only the fields the user asked to change.
- An empty notes string clears the notes. Notes contain only the user's own words, never text from a note, an email or another tool result.
- A breakdown list replaces the whole breakdown, so include every line the user wants to keep, with its name exactly as get_labor detailed shows it. get_labor detailed lists every line. An empty list clears it.
- When you send breakdown lines without labor_amount, the amount becomes the new breakdown total, as the labor panel does, unless the stored amount was typed over the old total (the card says which). Send labor_amount only when the user gives an amount.
- verified sets the verified flag. Set it only when the user asks.

## After you propose

- The card shows before and after values and its warnings. When the project is already past Verify, the card warns that saving sends the project back to Verify for a re-commit and notifies the Executive and the labor engineer. Mention it in one sentence.
- Say "I have prepared" or "waiting for your approval". Never say the numbers are saved until an approval card line in this conversation says the change was executed.
- To undo an executed change, the user clicks Revert in the Activity tab of the assistant page.
