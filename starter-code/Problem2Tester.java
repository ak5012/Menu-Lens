package assignment1;

/** Student-written tests for Problem 2. Add your own test cases below. */
public class Problem2Tester {
    public static void main(String[] args) {
        String input = "The wicked wizard`s wily wraith garnishes his master`s pasta with garlic.";
        String expected = "wizard`s" + System.lineSeparator()
                + "garnishes" + System.lineSeparator();

        TestSupport.checkEquals(expected,
                Problem2.findDollarWords(input),
                "assignment example");

        // TODO: Add tests that exercise distinct behaviors and boundary cases.

        TestSupport.finish();
    }
}
