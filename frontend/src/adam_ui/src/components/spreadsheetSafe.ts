//  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
//  SPDX-License-Identifier: Apache-2.0

// A spreadsheet runs a pasted cell that starts with one of these as a formula (OWASP, "CSV Injection").
const FORMULA_START = /^[=+\-@\t\r]/;
// A number can't be a formula, and a negative one must stay a number.
const NUMBER = /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/;

/** A cell copied as CSV, as a spreadsheet will show it rather than run it: query results are the database's
 * content, and an answer's tables are the model's, neither of them ours to let run in the user's spreadsheet. */
export function spreadsheetSafe(value: string): string {
  return FORMULA_START.test(value) && !NUMBER.test(value) ? `'${value}` : value;
}
