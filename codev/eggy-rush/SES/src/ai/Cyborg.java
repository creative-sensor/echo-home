/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package ai;

import core.TheMatrix;
import java.util.Arrays;
import java.util.Collections;
import java.util.Iterator;
import java.util.LinkedList;
import java.util.List;
import java.util.Random;
import java.util.Stack;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 *
 * @author Creativ
 */
public class Cyborg extends Thread {

    private TheMatrix matrix;
    int VertexNum = 0;
    private List<Cell> CellList; // mapping vertex ID to cell id;
    private boolean[][] amatrix;

    public Cyborg() {
        CellList = new LinkedList<>();
    }

    private void buildGraphModel() {
        // count number of vertex and record mapping
        for (int r = 0; r < 20; r++) {
            for (int c = 0; c < 32; c++) {
                if (!matrix.hasWall(r, c)) {
                    CellList.add(new Cell(r, c));
                    VertexNum++;
                }
            }
        }

        //System.out.print("cyborg: vertex num = " + VertexNum);

        // test adjacency
        amatrix = new boolean[VertexNum][VertexNum];
        for (int i = 0; i < VertexNum; i++) {
            for (int j = 0; j < VertexNum; j++) {
                Cell cellI = CellList.get(i);
                Cell cellJ = CellList.get(j);
                if (cellI.isNeighborOf(cellJ)) {
                    amatrix[i][j] = true;
                } else {
                    amatrix[i][j] = false;
                }
            }
        }


    }

    public void reference(TheMatrix m) {
        this.matrix = m;
        buildGraphModel();
    }

    private int min(int[] distance, boolean[] tentative) {
        int id = 0;
        int sentinel = distance[0];
        for (int i = 0; i < distance.length; i++) {
            if (tentative[i]) {
                id = i;
                sentinel = distance[i];
                break;
            }
        }

        for (int i = 0; i < distance.length; i++) {
            if ((distance[i] < sentinel) & (tentative[i])) {
                sentinel = distance[i];
                id = i;
            }
        }
        return id;

    }

    private Path computeShortestPath(Cell source, Cell destination) {
        // DIJKSTTRA ALGORITHM
        int sourceID = -1;
        int destinationID = -1;
        for (int i = 0; i < CellList.size(); i++) {
            Cell c = CellList.get(i);
            if (c.Row() == source.Row()) {
                if (c.Column() == source.Column()) {
                    sourceID = i;
                }
            }

            if (c.Row() == destination.Row()) {
                if (c.Column() == destination.Column()) {
                    destinationID = i;
                }
            }
        }


        //System.out.println("cyborg: source id = " + sourceID);
        //System.out.println("cyborg: dest id = " + destinationID);
        int[] distance = new int[VertexNum]; // distance from source in Dijkstra algorithm
        int[] predecessor = new int[VertexNum];
        boolean[] tentative = new boolean[VertexNum];
        int removeCount = 0;

        for (int i = 0; i < VertexNum; i++) {
            distance[i] = Path.INFINITY;
            tentative[i] = true;
            predecessor[i] = -1;
        }
        distance[sourceID] = 0;
        predecessor[sourceID] = sourceID;
        //removeCount++;

        while (removeCount < VertexNum) {
            for (int i = 0; i < tentative.length; i++) {
                if (!tentative[i]) {
                    for (int j = 0; j < VertexNum; j++) {
                        if (tentative[j]) {
                            if (amatrix[i][j]) {
                                if (distance[j] > distance[i] + 1) {
                                    distance[j] = distance[i] + 1;
                                    predecessor[j] = i;
                                }
                            }
                        }
                    }

                }
            }
            int removeid = (min(distance, tentative));
            tentative[removeid] = false;
            removeCount++;
        }
        // END OF DIJKSTRA ALGORITHM


        if (distance[destinationID] == Path.INFINITY) {
            return null;
        } else {
            //System.out.println("cyborg: distance[" + destinationID + "] = " + distance[destinationID]);
            Stack<Integer> directions;// list of direction in backward order
            directions = new Stack<>();
            int currentID = destinationID;
            int previousID = predecessor[destinationID];
            while (previousID != sourceID) {
                //System.out.println("cyborg: previous id = " + previousID);
                Cell currentCell = CellList.get(currentID);
                Cell previousCell = CellList.get(previousID);
                //System.out.println("cyborg: prev cell:  " + previousCell.Row() + " " + previousCell.Column());
                //System.out.println("cyborg: cur cell:  " + currentCell.Row() + " " + currentCell.Column());
                int dir = detectDirection(previousCell, currentCell);
                //System.out.println("cyborg: dir = " + dir);
                directions.push(dir);
                currentID = previousID;
                previousID = predecessor[previousID];
            }

            int finaldir = detectDirection(CellList.get(sourceID), CellList.get(currentID));
            directions.push(finaldir);
            directions.remove(0);

            int[] properDirection = new int[directions.size()];
            for (int i = 0; i < properDirection.length; i++) {
                properDirection[i] = directions.pop();
            }

            return new Path(properDirection, properDirection.length);
        }
    }

    private int detectDirection(Cell c1, Cell c2) { // c1 ---> c2

        if (c1.Row() == c2.Row()) {
            if (c1.Column() > c2.Column()) {
                return TheMatrix.LEFT;
            } else {
                return TheMatrix.RIGHT;
            }
        }

        if (c1.Column() == c2.Column()) {
            if (c1.Row() > c2.Row()) {
                return TheMatrix.DOWN;
            }
        }
        return TheMatrix.UP;
    }

    private List<Cell> probe() {
        List<Cell> list = new LinkedList<>();
        for (int i = 0; i < 20; i++) {
            for (int j = 0; j < 32; j++) {
                if (matrix.hasEgg(i, j)) {
                    if (matrix.getEggPhase(i, j) == 4) {
                        list.add(new Cell(i, j));
                        //System.out.println("cyborg: egg detected = " + i + "," + j);
                    }
                }
            }
        }
        return list;

    }

    private Path getOptimal(List<Path> l) {
        return Collections.min(l);
    }

    private void follow(Path p) {
        if (p == null) {
            return;
        }
        int direction = p.nextDirection();
        while (direction != Path.END) {
            switch (direction) {
                case TheMatrix.UP:
                    if (!matrix.moveUP()) {
                        return;
                    }
                    break;
                case TheMatrix.DOWN:
                    if(!matrix.moveDOWN()){
                        return;
                    };
                    break;
                case TheMatrix.LEFT:
                    if(!matrix.moveLEFT()){
                        return;
                    };
                    break;
                case TheMatrix.RIGHT:
                    if(!matrix.moveRIGHT()){
                        return;
                    };
                    break;
            }
            direction = p.nextDirection();
            try {
                Thread.sleep(2000);
            } catch (InterruptedException ex) {
                Logger.getLogger(Cyborg.class.getName()).log(Level.SEVERE, null, ex);
            }
        }
    }

    private void pickAtRandom() {
//        Random randgen = new Random(System.currentTimeMillis());
//        if (randgen.nextInt() > 2343) {
//            matrix.pick();
//        }
        matrix.pick();
    }

    @Override
    public void run() {
//        Evil Cell Problem is busted thanks to commented code!
//        Cell mycell = matrix.WhereAmI();
//        Path path1 = computeShortestPath(mycell, new Cell(0, 8));
//        follow(path1);
//        pickAtRandom();
//        mycell = matrix.WhereAmI();
//        Path path2 = computeShortestPath(mycell, new Cell(0, 12));
//        follow(path2);
//        pickAtRandom();

        while (true) {
            Cell myCell = matrix.WhereAmI();
            //System.out.println("my cell = " + myCell.Row() + "," + myCell.Column());
            List<Path> strategy = new LinkedList<>();

            List<Cell> result = probe();
            Iterator<Cell> foundEgg = result.iterator();
            while (foundEgg.hasNext()) {
                Path foundPath = computeShortestPath(myCell, foundEgg.next());

                if (foundPath != null) {
                    strategy.add(foundPath);
                    //System.out.println("cyborg: path found: cost = " + foundPath.getCost());
                }
            }
            if (strategy.isEmpty()) {
                continue;
            }
            follow(getOptimal(strategy));

            pickAtRandom();

//            Cell myCell = matrix.WhereAmI();
//            follow(computeShortestPath(myCell, new Cell(0, 25)));
//            follow(computeShortestPath(new Cell(0, 25), myCell));
        }
    }
}
