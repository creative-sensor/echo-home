/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package core;

import ai.Cell;
import java.util.Arrays;
import java.util.LinkedList;
import java.util.List;
import java.util.Queue;
import java.util.Random;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.logging.Level;
import java.util.logging.Logger;
import msg.BSSMessage;
import net.MessageTransport;
import ses.EggyRush;
import ses.TimeStamp;
import sync.SinghalHeuristic;

/**
 *
 * @author Creativ
 */
public class TheMatrix extends Thread {

    // DIRECTION
    public static final int UP = 0;
    public static final int DOWN = 1;
    public static final int LEFT = 2;
    public static final int RIGHT = 3;
    // ACTION
    static final int PICK = 1;
    static final int MOVE = 0;
    // OBSTACLES
    public static final int WALL = 1;
    public static final int NO_FOOTPRINT = -1;
    public static final int NO_EGG = -1;
    static final int UNDEFINED = -1; // no owner for an egg
    private Queue<BSSMessage> NetworkUpdateQueue;
    private SinghalHeuristic CellExclusion;
    private SinghalHeuristic EggGenSync;
    private EggGenerator eggGenerator;
    private Feeder feeder;
    private static int[] score;
    private int CurrentEggNum = 0;
    private int[] EggMap; // (row,column) --> id
    private int[] FootprintMap; // (row,column) --> id
    private List<Egg> EggCollection; // id --> (row,column, picker id, time of picking)
    private List<Footprint> fpLocation; // id --> (row,column)
    private static int[] maze = {0, 1, 1, 1, 1, 1, 1, 1, 0, 1, 1, 1, 0, 1, 1, 0, 0, 1, 1, 1, 1, 1, 0, 1, 1, 0, 0, 0, 1, 0, 1, 0,
        0, 1, 1, 1, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0,
        0, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 1, 1, 1, 0, 0, 1, 1, 0, 1, 1, 0, 0, 0, 1,
        0, 0, 0, 1, 1, 0, 1, 1, 1, 0, 1, 0, 1, 0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 0, 1,
        1, 1, 0, 1, 1, 0, 0, 0, 1, 0, 0, 1, 0, 1, 0, 1, 1, 0, 1, 0, 0, 0, 1, 1, 0, 0, 1, 0, 0, 1, 0, 1,
        0, 0, 0, 0, 0, 0, 1, 0, 1, 1, 0, 0, 1, 1, 0, 0, 0, 1, 0, 1, 1, 0, 0, 0, 1, 0, 0, 1, 0, 1, 0, 0,
        1, 1, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0,
        1, 1, 1, 1, 0, 1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 1, 1, 0, 0, 1, 0, 0, 0, 0, 1, 0, 1, 0, 0,
        1, 1, 1, 1, 0, 0, 0, 1, 0, 1, 1, 0, 1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 1, 1, 0, 1, 0, 1, 0, 0,
        1, 0, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 1, 1, 0, 0, 1, 1, 0, 1, 0, 1, 0, 1, 1, 1, 1, 0, 1, 0, 0,
        1, 0, 0, 0, 0, 1, 0, 1, 1, 1, 1, 0, 1, 1, 0, 0, 0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 0, 1, 0, 1, 1, 0,
        0, 0, 1, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 1, 1, 0, 0, 0, 1, 0, 1, 1, 0, 0, 0,
        0, 1, 0, 0, 0, 1, 1, 0, 1, 0, 1, 0, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0,
        0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 1, 0, 1, 1, 0,
        0, 0, 1, 1, 0, 1, 0, 1, 1, 1, 0, 0, 0, 1, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 0, 0,
        1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 0, 1, 1, 1, 0, 1, 0, 1, 0, 1, 0, 0, 0, 1,
        1, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 1, 0, 0, 1, 1, 0, 1, 0, 1, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1,
        1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1, 1, 0, 1, 1, 1, 1, 0, 0, 0, 0,
        1, 0, 1, 0, 1, 1, 0, 0, 1, 1, 0, 1, 1, 0, 0, 0, 0, 1, 1, 0, 1, 0, 1, 1, 0, 0, 1, 0, 0, 1, 1, 0,
        0, 0, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 1, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 1, 0};
    private MessageTransport messageTransport;

    public void referenceCellME(SinghalHeuristic c) {
        this.CellExclusion = c;
    }

    public void referenceEggGenME(SinghalHeuristic e) {
        this.EggGenSync = e;
        eggGenerator.reference(EggGenSync);
    }

    public void reference(MessageTransport mt) {
        this.messageTransport = mt;
        eggGenerator.reference(messageTransport);
    }

    public void reference(Queue<BSSMessage> NetworkUpdateQueue) {
        this.NetworkUpdateQueue = NetworkUpdateQueue;
    }

    public int getEggPhase(int row, int col) {
        int id = EggMap[row * 32 + col];
        return this.EggCollection.get(id).Phase();
    }

    public void put(Egg newEgg) {
        this.EggCollection.add(newEgg);
        this.EggMap[newEgg.Row() * 32 + newEgg.Column()] = this.EggCollection.size() - 1;
    }

    private static void transformTheMaze() {
        for (int i = 0; i < 10; i++) { // for first upper row
            for (int j = 0; j < 32; j++) { // for each column
                int temp = maze[i * 32 + j];
                maze[i * 32 + j] = maze[(19 - i) * 32 + j];
                maze[(19 - i) * 32 + j] = temp;
            }
        }
    }

    public boolean hasWall(int i, int j) {
        if (maze[i * 32 + j] == TheMatrix.WALL) {
            return true;
        }
        return false;
    }

    public boolean hasEgg(int i, int j) {
        if (EggMap[i * 32 + j] != TheMatrix.NO_EGG) {
            return true;
        }
        return false;
    }

    public boolean hasFootprint(int i, int j) {
        if (FootprintMap[i * 32 + j] != TheMatrix.NO_FOOTPRINT) {
            return true;
        }
        return false;
    }

    public int getFootprintID(int i, int j) {
        return this.FootprintMap[i * 32 + j];
    }

    public int[] getScoreReference() {
        return this.score;
    }

    public Cell WhereAmI() {
        return new Cell(fpLocation.get(EggyRush.myID));
    }

    private void initialize() {
        NetworkUpdateQueue = new LinkedBlockingQueue<>();
        // CREATE MAPS
        EggMap = new int[32 * 20];
        FootprintMap = new int[32 * 20];
        for (int i = 0; i < FootprintMap.length; i++) {
            FootprintMap[i] = NO_FOOTPRINT;
            EggMap[i] = NO_EGG;
        }

        // PUT EGG AND FOOTPRINT ON THE MAPS
        EggCollection = new LinkedList<>();
        for (int i = 0; i < EggyRush.PlayNum * 3; i++) {
        }
        fpLocation = new LinkedList<>();
        score = new int[EggyRush.PlayNum];

        for (int p = 0; p < EggyRush.PlayNum; p++) {
            for (int i = 32 * 20 - 1; i >= 0; i--) {
                if (maze[i] != WALL) {
                    if (FootprintMap[i] == NO_FOOTPRINT) {
                        fpLocation.add(new Footprint(i / 32, i % 32));
                        FootprintMap[i] = p;
                        break;
                    }
                }

            }
        }

        // Create Eggs

        for (int e = 0; e < Math.max(5, 2 * EggyRush.PlayNum); e++) {
            for (int i = 0; i < 32 * 20; i++) {
                if (maze[i] != WALL) {
                    if (FootprintMap[i] == NO_FOOTPRINT) {
                        if (EggMap[i] == NO_EGG) {
                            EggMap[i] = e;
                            EggCollection.add(new Egg(i / 32, i % 32));
                            break;

                        }
                    }
                }
            }
        }

        // run engine
        this.eggGenerator = new EggGenerator();
        this.feeder = new Feeder();

        eggGenerator.reference(EggCollection);
        eggGenerator.reference(maze, FootprintMap, EggMap);
        feeder.reference(EggCollection);

        eggGenerator.start();
        feeder.start();

    }

    public TheMatrix() {
        initialize();
    }

    private boolean detectCollision(int row, int col, int direction) {
        switch (direction) {
            case UP:
                if (col - 1 >= 0) { // left cell exists
                    if (FootprintMap[(row + 1) * 32 + col - 1] != NO_FOOTPRINT) {
                        return true;

                    }
                }
                if (col + 1 < 32) { // right cell exists
                    if (FootprintMap[(row + 1) * 32 + col + 1] != NO_FOOTPRINT) {
                        return true;
                    }
                }
                if (row + 2 < 20) { // cell above exists
                    if (FootprintMap[(row + 2) * 32 + col] != NO_FOOTPRINT) {
                        return true;
                    }
                }
                break;
            case DOWN:
                if (col - 1 >= 0) {
                    if (FootprintMap[(row - 1) * 32 + col - 1] != NO_FOOTPRINT) {
                        return true;

                    }
                }
                if (col + 1 < 32) {
                    if (FootprintMap[(row - 1) * 32 + col + 1] != NO_FOOTPRINT) {
                        return true;
                    }
                }
                if (row - 2 >= 0) {
                    if (FootprintMap[(row - 2) * 32 + col] != NO_FOOTPRINT) {
                        return true;

                    }
                }
                break;
            case LEFT:
                if (row - 1 >= 0) {
                    if (FootprintMap[(row - 1) * 32 + col - 1] != NO_FOOTPRINT) {
                        return true;
                    }
                }
                if (row + 1 < 20) {
                    if (FootprintMap[(row + 1) * 32 + col - 1] != NO_FOOTPRINT) {
                        return true;
                    }
                }
                if (col - 2 >= 0) {
                    if (FootprintMap[row * 32 + col - 2] != NO_FOOTPRINT) {
                        return true;

                    }
                }
                break;
            case RIGHT:
                if (row - 1 >= 0) {
                    if (FootprintMap[(row - 1) * 32 + col + 1] != NO_FOOTPRINT) {
                        return true;
                    }
                }
                if (row + 1 < 20) {
                    if (FootprintMap[(row + 1) * 32 + col + 1] != NO_FOOTPRINT) {
                        return true;
                    }
                }
                if (col + 2 < 32) {
                    if (FootprintMap[row * 32 + col + 2] != NO_FOOTPRINT) {
                        return true;
                    }
                }
                break;
        }
        return false;
    }

    public boolean moveUP() {
        // check obstacle
        // detect collision on Critical Section
        Footprint myfootprint = fpLocation.get(EggyRush.myID);
        int row = myfootprint.Row();
        int col = myfootprint.Column();
        if ((row + 1 < 20) && (TheMatrix.maze[(row + 1) * 32 + col] != WALL)) {
            if (this.FootprintMap[(row + 1) * 32 + col] != NO_FOOTPRINT) {
                return false;
            }
            if (this.EggMap[(row + 1) * 32 + col] != NO_EGG) {
                int id = this.EggMap[(row + 1) * 32 + col];
                if (this.EggCollection.get(id).Phase() == 4) {
                    return false;
                }
            }
            if (detectCollision(row, col, UP)) {
                System.out.println("WARNING: Possible collision detected!");
                CellExclusion.request();
                CellExclusion.printToken();
                if (FootprintMap[(row + 1) * 32 + col] != NO_FOOTPRINT) {
                    CellExclusion.release();
                    return false;
                } else {
                    FootprintMap[row * 32 + col] = NO_FOOTPRINT;
                    myfootprint.setRow(++row);
                    FootprintMap[row * 32 + col] = EggyRush.myID;
                    messageTransport.broadcast(EggyRush.myID, MOVE, row, col);
                    CellExclusion.release();
                    System.out.println("OBJECT moved up: [" + row + "," + col + "]");
                    return true;
                }
            }
            FootprintMap[row * 32 + col] = NO_FOOTPRINT;
            myfootprint.setRow(++row);
            FootprintMap[row * 32 + col] = EggyRush.myID;
            messageTransport.broadcast(EggyRush.myID, MOVE, row, col);

        }

        System.out.println("OBJECT moved up: [" + row + "," + col + "]");
        return true;

    }

    public boolean moveDOWN() {
        Footprint myfootprint = fpLocation.get(EggyRush.myID);
        int row = myfootprint.Row();
        int col = myfootprint.Column();

        if ((row - 1 >= 0) && (maze[(row - 1) * 32 + col] != WALL)) {
            if (this.FootprintMap[(row - 1) * 32 + col] != NO_FOOTPRINT) {
                return false;
            }
            if (this.EggMap[(row - 1) * 32 + col] != NO_EGG) {
                int id = this.EggMap[(row - 1) * 32 + col];
                if (this.EggCollection.get(id).Phase() == 4) {
                    return false;
                }
            }
            if (detectCollision(row, col, DOWN)) {
                System.out.println("WARNING: Possible collision detected!");
                CellExclusion.request();
                CellExclusion.printToken();
                if (FootprintMap[(row - 1) * 32 + col] != NO_FOOTPRINT) {
                    CellExclusion.release();
                    return false;
                } else {
                    FootprintMap[row * 32 + col] = NO_FOOTPRINT;
                    myfootprint.setRow(--row);
                    FootprintMap[row * 32 + col] = EggyRush.myID;
                    messageTransport.broadcast(EggyRush.myID, MOVE, row, col);
                    CellExclusion.release();
                    System.out.println("OBJECT moved down: [" + row + "," + col + "]");
                    return true;
                }
            }
            FootprintMap[row * 32 + col] = NO_FOOTPRINT;
            myfootprint.setRow(--row);
            FootprintMap[row * 32 + col] = EggyRush.myID;
            messageTransport.broadcast(EggyRush.myID, MOVE, row, col);

        }

        System.out.println("OBJECT moved down: [" + row + "," + col + "]");
        return true;

    }

    public boolean moveLEFT() {
        Footprint myfootprint = fpLocation.get(EggyRush.myID);
        int row = myfootprint.Row();
        int col = myfootprint.Column();

        if ((col - 1 >= 0) && (maze[row * 32 + col - 1] != WALL)) {
            if (this.FootprintMap[row * 32 + col - 1] != NO_FOOTPRINT) {
                return false;
            }
            if (this.EggMap[row * 32 + col - 1] != NO_EGG) {
                int id = this.EggMap[row * 32 + col - 1];
                if (this.EggCollection.get(id).Phase() == 4) {
                    return false;
                }
            }
            if (detectCollision(row, col, LEFT)) {
                System.out.println("WARNING: Possible collision detected!");
                CellExclusion.request();
                CellExclusion.printToken();
                if (FootprintMap[row * 32 + col - 1] != NO_FOOTPRINT) {
                    CellExclusion.release();
                    return false;
                } else {
                    FootprintMap[row * 32 + col] = NO_FOOTPRINT;
                    myfootprint.setColumn(--col);
                    FootprintMap[row * 32 + col] = EggyRush.myID;
                    messageTransport.broadcast(EggyRush.myID, MOVE, row, col);
                    CellExclusion.release();
                    System.out.println("OBJECT moved left: [" + row + "," + col + "]");
                    return true;
                }
            }
            FootprintMap[row * 32 + col] = NO_FOOTPRINT;
            myfootprint.setColumn(--col);
            FootprintMap[row * 32 + col] = EggyRush.myID;
            messageTransport.broadcast(EggyRush.myID, MOVE, row, col);
        }
        System.out.println("OBJECT moved left: [" + row + "," + col + "]");
        return true;

    }

    public boolean moveRIGHT() {
        Footprint myfootprint = fpLocation.get(EggyRush.myID);
        int row = myfootprint.Row();
        int col = myfootprint.Column();

        if ((col + 1 < 32) && (maze[row * 32 + col + 1] != WALL)) {
            if (this.FootprintMap[row * 32 + col + 1] != NO_FOOTPRINT) {
                return false;
            }
            if (this.EggMap[row * 32 + col + 1] != NO_EGG) {
                int id = this.EggMap[row * 32 + col + 1];
                if (this.EggCollection.get(id).Phase() == 4) {
                    return false;
                }
            }
            if (detectCollision(row, col, RIGHT)) {
                System.out.println("WARNING: Possible collision detected!");
                CellExclusion.request();
                CellExclusion.printToken();
                if (FootprintMap[row * 32 + col + 1] != NO_FOOTPRINT) {
                    CellExclusion.release();
                    return false;
                } else {
                    FootprintMap[row * 32 + col] = NO_FOOTPRINT;
                    myfootprint.setColumn(++col);
                    FootprintMap[row * 32 + col] = EggyRush.myID;
                    messageTransport.broadcast(EggyRush.myID, MOVE, row, col);
                    CellExclusion.release();
                    System.out.println("OBJECT moved right: [" + row + "," + col + "]");
                    return true;
                }
            }
            FootprintMap[row * 32 + col] = NO_FOOTPRINT;
            myfootprint.setColumn(++col);
            FootprintMap[row * 32 + col] = EggyRush.myID;
            messageTransport.broadcast(EggyRush.myID, MOVE, row, col);

        }
        System.out.println("OBJECT moved right: [" + row + "," + col + "]");

        return true;
    }

    public void pick() {
        Footprint myfootprint = fpLocation.get(EggyRush.myID);
        int row = myfootprint.Row();
        int col = myfootprint.Column();
        for (int i = row - 1; i <= row + 1; i++) {
            if ((i >= 0) && (i < 20)) {
                for (int j = col - 1; j <= col + 1; j++) {
                    if ((j >= 0) && (j < 32)) {
                        synchronized (EggMap) {
                            int eggid = EggMap[i * 32 + j];
                            if (eggid != NO_EGG) {
                                EggMap[i * 32 + j] = NO_EGG;
                                TimeStamp ts = messageTransport.broadcast(EggyRush.myID, PICK, eggid, 0);
                                EggCollection.get(eggid).setPickerID(EggyRush.myID);
                                EggCollection.get(eggid).setPickTime(ts);
                                System.out.println("OBJECT picked up egg[" + eggid + "] at time " + Arrays.toString(ts.value()));
                                eggGenerator.prompt();
                                score[EggyRush.myID]++;
                                synchronized (score) {
                                    score.notify();
                                }
                                return;
                            }
                        }
                    }
                }
            }
        }
    }

    @Override
    public void run() {
        while (true) {
            BSSMessage msg;
            synchronized (NetworkUpdateQueue) {
                if (NetworkUpdateQueue.isEmpty()) {
                    try {
                        NetworkUpdateQueue.wait();
                    } catch (InterruptedException ex) {
                        Logger.getLogger(TheMatrix.class.getName()).log(Level.SEVERE, null, ex);
                    }
                }
            }
            msg = NetworkUpdateQueue.remove();
            String content = msg.getContent(); // content = [ID,ACTION, PARA, PARA]
            String extractedContent = content.substring(1, content.length() - 1);
            String[] fields = extractedContent.split(", "); // DANGEROUS!!!

            TimeStamp subjectPickTime = msg.getTimeStamp();
            int subjectID = Integer.parseInt(fields[0]);
            int action = Integer.parseInt(fields[1]);
            int eggID = Integer.parseInt(fields[2]);
            int newrow = Integer.parseInt(fields[2]);
            int newcol = Integer.parseInt(fields[3]);

            switch (action) {
                case MOVE:
                    int currow = fpLocation.get(subjectID).Row();
                    int curcol = fpLocation.get(subjectID).Column();
                    FootprintMap[currow * 32 + curcol] = NO_FOOTPRINT;
                    FootprintMap[newrow * 32 + newcol] = subjectID;
                    fpLocation.get(subjectID).setRow(newrow);
                    fpLocation.get(subjectID).setColumn(newcol);
                    System.out.println("Player " + subjectID + " moves to" + " [" + newrow + "," + newcol + "]");
                    break;
                case PICK:
                    Egg pickedEgg = EggCollection.get(eggID);
                    int row = pickedEgg.Row();
                    int col = pickedEgg.Column();
                    if (pickedEgg.getPickerID() == TheMatrix.UNDEFINED) { // no one has already picked this egg
                        pickedEgg.setPickerID(subjectID); // make subjectID the owner of the egg
                        pickedEgg.setPickTime(subjectPickTime); // record the time at which the subject picked this egg
                        TheMatrix.score[subjectID]++;
                        EggMap[row * 32 + col] = NO_EGG;
                        System.out.println("Player " + subjectID + " picked up egg[" + eggID + "] at time " + Arrays.toString(subjectPickTime.value()));
                    } else {
                        boolean EggPickerNotLessThanSubject = pickedEgg.getPickTime().isNotLessThan(subjectPickTime);
                        boolean SubjectNotLessThanEggPicker = subjectPickTime.isNotLessThan(pickedEgg.getPickTime());

                        if (EggPickerNotLessThanSubject && SubjectNotLessThanEggPicker) {
                            score[pickedEgg.getPickerID()]--;
                            score[subjectID]--;
                            break;
                        }
                        if (EggPickerNotLessThanSubject) { // someone have picked this egg two but not sooner than subject
                            score[pickedEgg.getPickerID()]--; //recall score of the player who picked this egg 
                            if (pickedEgg.getPickTime().isEqualTo(subjectPickTime)) { // the player picked this egg the same time with subject ID
                                // recall scores for both picker
                                score[subjectID]--;
                                score[pickedEgg.getPickerID()]--;
                            } else {
                                pickedEgg.setPickerID(subjectID);
                                pickedEgg.setPickTime(subjectPickTime);
                                score[subjectID]++;
                            }
                            System.out.println("Player " + subjectID + " picked up egg[" + eggID + "] at time " + Arrays.toString(subjectPickTime.value()));
                        }
                    }
                    synchronized (score) {
                        score.notify();
                    }
                    break;
            }


        }
    }
}
