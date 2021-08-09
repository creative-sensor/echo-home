/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package ai;

/**
 *
 * @author Creativ
 */
public class Path implements Comparable<Path> {

    public static final int INFINITY = 700;
    public static final int END = -1;
    int[] directions;
    int cursor = 0;
    int cost;

    public Path(int[] directions, int cost) {
        this.directions = directions;
        this.cost = cost;
    }

    public boolean isInfinte() {
        if (cost == INFINITY) {
            return true;
        }

        return false;
    }

    public int getCost() {
        return this.cost;
    }

    @Override
    public int compareTo(Path o) {
        return this.cost - o.getCost();

    }

    public void resetCursor() {
        this.cursor = -1;
    }

    public int nextDirection() {
        if (cursor >= directions.length) {
            return END;
        }
        int dir = this.directions[cursor];
        cursor++;
        return dir;
    }
}
