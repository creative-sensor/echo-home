/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package visual;

import core.TheMatrix;
import java.util.logging.Level;
import java.util.logging.Logger;
import org.lwjgl.LWJGLException;
import org.lwjgl.input.Keyboard;
import org.lwjgl.opengl.Display;
import org.lwjgl.opengl.DisplayMode;
import org.lwjgl.opengl.GL11;

/**
 *
 * @author Creativ
 */
public class SimpleGraphics extends Thread {

    private final int ROOT_X = 80; // root of game display region in OpenGL coordination
    private final int ROOT_Y = 100; // root of game display region in OpenGL coordination
    //COLORING
    static final float[] WALL_COLOR = {0.1f, .6f, 1f};
    static final float[] FOOTPRINT_COLOR = {0f, 0f, 0f};
    static final float[] GROUND_COLOR = {1f, 1f, 1f};
    static final float[] EGG_PHASE_1_COLOR = {1f, 0.8f, 0.2f};
    static final float[] EGG_PHASE_2_COLOR = {0.96f, 0.72f, 0f};
    static final float[] EGG_PHASE_3_COLOR = {0.72f, 0.54f, 0f};
    static final float[] EGG_PHASE_4_COLOR = {1f, 0.4f, 0.2f};
    static final float[] NUMBER_COLOR = {0.8f, 1f, 0.2f};
    private TheMatrix matrix;

    public void reference(TheMatrix m) {
        this.matrix = m;
    }

    public SimpleGraphics() {
    }

    // MAIN LOOP OF RENDERING
    @Override
    public void run() {
        try {
            Display.setDisplayMode(new DisplayMode(800, 600));
            //Display.setInitialBackground(0.5f, 0.3f, 0.1f);
            //Display.setVSyncEnabled(true);
            Display.create();

            GL11.glMatrixMode(GL11.GL_PROJECTION);
            GL11.glLoadIdentity();
            GL11.glOrtho(0, 800, 0, 600, 1, -1);
            GL11.glMatrixMode(GL11.GL_MODELVIEW);


            while (!Display.isCloseRequested()) {
                captureEvent();
                paintGround();
                paintAllObjects();
                Display.update();
                //Display.sync(60);
            }
            Display.destroy();
        } catch (LWJGLException ex) {
            Logger.getLogger(SimpleGraphics.class.getName()).log(Level.SEVERE, null, ex);
        }

    }

    private void paintGround() {
        GL11.glColor3f(1f, 1f, 1f);
        GL11.glRecti(ROOT_X - 5, ROOT_Y - 5, ROOT_X + 20 * 32 + 5, ROOT_Y + 20 * 20 + 5);
        GL11.glFlush();
    }

    private void paintAllObjects() {
        for (int i = 0; i < 20; i++) {// row
            for (int j = 0; j < 32; j++) { // column
                if (matrix.hasWall(i, j)) {
                    paintCell(i, j, WALL_COLOR);
                }

                if (matrix.hasEgg(i, j)) {
                    switch (matrix.getEggPhase(i, j)) {
                        case 1:
                            paintCell(i, j, EGG_PHASE_1_COLOR);
                            break;
                        case 2:
                            paintCell(i, j, EGG_PHASE_2_COLOR);
                            break;
                        case 3:
                            paintCell(i, j, EGG_PHASE_3_COLOR);
                            break;
                        case 4:
                            paintCell(i, j, EGG_PHASE_4_COLOR);
                            break;
                    }

                }

                if (matrix.hasFootprint(i, j)) {
                    paintCell(i, j, FOOTPRINT_COLOR);
                    switch (matrix.getFootprintID(i, j)) {
                        case 0:
                            paintNumber0(i, j, NUMBER_COLOR);
                            break;
                        case 1:
                            paintNumber1(i, j, NUMBER_COLOR);
                            break;
                        case 2:
                            paintNumber2(i, j, NUMBER_COLOR);
                            break;
                    }

                }
            }
        }
    }

    private void paintCell(int RowIndex, int ColumnIndex, float[] color) {
        int x = ROOT_X + ColumnIndex * 20; // damp thing goes here
        int y = ROOT_Y + RowIndex * 20; // 20 is cell size
        //GL11.glClear(GL11.GL_COLOR_BUFFER_BIT | GL11.GL_DEPTH_BUFFER_BIT);
        GL11.glColor3f(color[0], color[1], color[2]);
        GL11.glRecti(x + 1, y + 1, x + 19, y + 19); // render cell a little smaller
        GL11.glFlush();

    }

    private void captureEvent() {
        while (Keyboard.next()) {
            if (Keyboard.getEventKeyState()) {
                int key = Keyboard.getEventKey();
                switch (key) {
                    case Keyboard.KEY_DOWN:
                        boolean moveDOWN = matrix.moveDOWN();
                        break;
                    case Keyboard.KEY_UP:
                        boolean moveUP = matrix.moveUP();
                        break;
                    case Keyboard.KEY_LEFT:
                        boolean moveLEFT = matrix.moveLEFT();
                        break;
                    case Keyboard.KEY_RIGHT:
                        boolean moveRIGHT = matrix.moveRIGHT();
                        break;
                    case Keyboard.KEY_SPACE:
                        matrix.pick();

                }
            }
        }

    }

    private void paintNumber0(int RowIndex, int ColumnIndex, float[] color) {
        int x = ROOT_X + ColumnIndex * 20; // damp thing goes here
        int y = ROOT_Y + RowIndex * 20; // 20 is cell size
        //GL11.glClear(GL11.GL_COLOR_BUFFER_BIT | GL11.GL_DEPTH_BUFFER_BIT);
        GL11.glColor3f(color[0], color[1], color[2]);
        GL11.glRecti(x + 4, y + 2, x + 15, y + 4); // render cell a little smaller
        GL11.glRecti(x + 4, y + 15, x + 15, y + 17);

        GL11.glRecti(x + 4, y + 2, x + 6, y + 17); // render cell a little smaller
        GL11.glRecti(x + 13, y + 2, x + 15, y + 17);

        GL11.glFlush();
    }

    private void paintNumber1(int RowIndex, int ColumnIndex, float[] color) {
        int x = ROOT_X + ColumnIndex * 20; // damp thing goes here
        int y = ROOT_Y + RowIndex * 20; // 20 is cell size
        //GL11.glClear(GL11.GL_COLOR_BUFFER_BIT | GL11.GL_DEPTH_BUFFER_BIT);
        GL11.glColor3f(color[0], color[1], color[2]);
        GL11.glRecti(x + 4, y + 2, x + 16, y + 5); // render cell a little smaller
        GL11.glRecti(x + 8, y + 2, x + 12, y + 17);
        GL11.glRecti(x + 4, y + 13, x + 12, y + 14); // render cell a little smaller
        GL11.glRecti(x + 6, y + 13, x + 8, y + 16);
        GL11.glFlush();
    }

    private void paintNumber2(int RowIndex, int ColumnIndex, float[] color) {
        int x = ROOT_X + ColumnIndex * 20; // damp thing goes here
        int y = ROOT_Y + RowIndex * 20; // 20 is cell size
        //GL11.glClear(GL11.GL_COLOR_BUFFER_BIT | GL11.GL_DEPTH_BUFFER_BIT);
        GL11.glColor3f(color[0], color[1], color[2]);
        GL11.glRecti(x + 3, y + 3, x + 16, y + 5); // render cell a little smaller
        GL11.glRecti(x + 3, y + 9, x + 16, y + 11);
        GL11.glRecti(x + 3, y + 15, x + 16, y + 17); // render cell a little smaller
        GL11.glRecti(x + 3, y + 3, x + 6, y + 11);
        GL11.glRecti(x + 14, y + 9, x + 16, y + 17);
        GL11.glFlush();
    }
}
